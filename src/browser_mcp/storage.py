"""Durable SQLite storage for browser task records and their event history."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .domain import DomainError, TASK_TRANSITIONS, TaskRecord, TaskSpec, TaskStatus


_REDACT_KEY = re.compile(r"secret|password|passwd|token|cookie|authorization|credential|api[_-]?key", re.I)
_REDACT_INLINE = re.compile(r'\b(?:secret|password|passwd|token|cookie|authorization|credential|api[_-]?key)\s*[:=]\s*\S+', re.I)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clean(value: Any) -> Any:
    """Remove values under obviously sensitive keys before writing event payloads."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _REDACT_KEY.search(str(k)) else _clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class TaskRepository:
    """Thread-safe repository. Each operation uses a short SQLite transaction."""

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    phase TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT,
                    result_json TEXT,
                    UNIQUE(principal_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events_task_id_event_id ON task_events(task_id, event_id);
                CREATE TABLE IF NOT EXISTS task_capabilities (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    capability TEXT NOT NULL,
                    grant_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(task_id, capability)
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise DomainError("REPOSITORY_CLOSED", "task repository is closed")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"], principal_id=row["principal_id"],
            spec=TaskSpec.model_validate_json(row["spec_json"]), status=row["status"],
            version=row["version"], phase=row["phase"], created_at=row["created_at"],
            updated_at=row["updated_at"], step_count=row["step_count"],
            error=json.loads(row["error_json"]) if row["error_json"] is not None else None,
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        )

    @staticmethod
    def _event(conn: sqlite3.Connection, task_id: str, event_type: str, payload: Any) -> dict[str, Any]:
        cursor = conn.execute(
            "INSERT INTO task_events(task_id,event_type,timestamp,payload_json) VALUES(?,?,?,?)",
            (task_id, event_type, _utc_now(), _json(_clean(payload))),
        )
        row = conn.execute("SELECT * FROM task_events WHERE event_id=?", (cursor.lastrowid,)).fetchone()
        return {"event_id": row["event_id"], "task_id": row["task_id"], "event_type": row["event_type"],
                "timestamp": row["timestamp"], "payload": json.loads(row["payload_json"])}

    def create(self, spec: TaskSpec, principal_id: str = "operator") -> TaskRecord:
        canonical = _json(spec.model_dump(mode="json"))
        with self._lock, self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tasks WHERE principal_id=? AND idempotency_key=?",
                (principal_id, spec.idempotency_key),
            ).fetchone()
            if existing:
                if _json(TaskSpec.model_validate_json(existing["spec_json"]).model_dump(mode="json")) != canonical:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "idempotency key was already used for a different task")
                return self._record(existing)
            now = _utc_now()
            task_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO tasks(task_id,principal_id,idempotency_key,spec_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, principal_id, spec.idempotency_key, canonical, TaskStatus.QUEUED.value, 1, now, now),
            )
            self._event(conn, task_id, "task.created", {"status": TaskStatus.QUEUED.value})
            for capability in spec.site_capabilities:
                conn.execute("INSERT INTO task_capabilities(task_id,capability,grant_id) VALUES(?,?,?)", (task_id, capability, str(uuid.uuid4())))
                self._event(conn, task_id, "task.capability_granted", {"capability": capability})
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._record(row)

    def grant_capability(self, task_id: str, capability: str, expected_version: int) -> dict[str, str]:
        if capability != 'gemini_prompt_submit':
            raise DomainError('CAPABILITY_INVALID', 'Неизвестное разрешение')
        with self._lock, self._transaction() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise DomainError('TASK_NOT_FOUND', 'Задача не найдена')
            if expected_version != row['version']:
                raise DomainError('VERSION_CONFLICT', 'Состояние задачи изменилось')
            if TaskStatus(row['status']) in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}:
                raise DomainError('INVALID_TRANSITION', 'Задача уже завершена')
            if 'https://gemini.google.com' not in TaskSpec.model_validate_json(row['spec_json']).allowed_origins:
                raise DomainError('POLICY_DENIED', 'Gemini не входит в разрешённые сайты задачи')
            existing = conn.execute('SELECT grant_id FROM task_capabilities WHERE task_id=? AND capability=?', (task_id, capability)).fetchone()
            if existing:
                return {'kind': capability, 'grant_id': existing['grant_id']}
            grant_id = str(uuid.uuid4())
            conn.execute('INSERT INTO task_capabilities(task_id,capability,grant_id) VALUES(?,?,?)', (task_id, capability, grant_id))
            conn.execute('UPDATE tasks SET version=version+1, updated_at=? WHERE task_id=?', (_utc_now(), task_id))
            self._event(conn, task_id, 'task.capability_granted', {'capability': capability})
            return {'kind': capability, 'grant_id': grant_id}

    def capability(self, task_id: str, capability: str) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute('SELECT grant_id FROM task_capabilities WHERE task_id=? AND capability=?', (task_id, capability)).fetchone()
            return {'kind': capability, 'grant_id': row['grant_id']} if row else None

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            if self._closed:
                raise DomainError("REPOSITORY_CLOSED", "task repository is closed")
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            return self._record(row)

    def list(self, principal_id: str = "operator", limit: int = 100) -> list[TaskRecord]:
        if not 1 <= limit <= 1000:
            raise DomainError("INVALID_LIMIT", "limit must be between 1 and 1000")
        with self._lock:
            if self._closed:
                raise DomainError("REPOSITORY_CLOSED", "task repository is closed")
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE principal_id=? ORDER BY created_at DESC, task_id LIMIT ?",
                (principal_id, limit),
            ).fetchall()
            return [self._record(row) for row in rows]

    def transition(
        self, task_id: str, status: TaskStatus | str, expected_version: int | None = None,
        phase: str | None = None, error: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskRecord:
        try:
            target = TaskStatus(status)
        except ValueError as exc:
            raise DomainError("INVALID_STATUS", "unknown task status") from exc
        with self._lock, self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            current = TaskStatus(row["status"])
            if expected_version is not None and expected_version != row["version"]:
                raise DomainError("VERSION_CONFLICT", "task version does not match expected_version")
            if target not in TASK_TRANSITIONS[current]:
                raise DomainError("INVALID_TRANSITION", f"cannot transition task from {current.value} to {target.value}")
            now = _utc_now()
            conn.execute(
                "UPDATE tasks SET status=?,version=version+1,phase=COALESCE(?,phase),updated_at=?,error_json=COALESCE(?,error_json),result_json=COALESCE(?,result_json) WHERE task_id=?",
                (target.value, phase, now, _json(_clean(error)) if error is not None else None,
                 _json(_clean(result)) if result is not None else None, task_id),
            )
            self._event(conn, task_id, "task.status_changed", {"from": current.value, "to": target.value, "phase": phase})
            updated = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._record(updated)

    def add_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not event_type or len(event_type) > 128:
            raise DomainError("INVALID_EVENT_TYPE", "event_type must contain 1 to 128 characters")
        with self._lock, self._transaction() as conn:
            if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            return self._event(conn, task_id, event_type, payload)

    def autonomy_notice(self, task_id: str, question: str) -> tuple[int, int]:
        with self._lock, self._transaction() as conn:
            row = conn.execute('SELECT status,step_count,spec_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None: raise DomainError('TASK_NOT_FOUND', 'Задача не найдена')
            if row['status'] != TaskStatus.RUNNING.value: raise DomainError('INVALID_STATUS', 'Задача не выполняется')
            total = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='attention.notice'", (task_id,)).fetchone()[0]
            if total >= 8: return total, 3
            next_count = row['step_count'] + 1
            if next_count > TaskSpec.model_validate_json(row['spec_json']).limits.max_steps:
                raise DomainError('STEP_LIMIT_EXCEEDED', 'task step limit has been reached')
            safe_message = _REDACT_INLINE.sub('[скрыто]', question[:1000])
            self._event(conn, task_id, 'attention.notice', {'message': safe_message})
            conn.execute('UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?', (next_count, _utc_now(), task_id))
            self._event(conn, task_id, 'task.step_counted', {'step_count': next_count})
            consecutive = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='attention.notice' AND event_id > COALESCE((SELECT MAX(event_id) FROM task_events WHERE task_id=? AND event_type='action.applied'),0)", (task_id, task_id)).fetchone()[0]
            return total + 1, consecutive

    def record_editable_focus_rejection(self, task_id: str) -> int:
        """Record a proven pre-input rejection and spend one bounded task step."""
        with self._lock, self._transaction() as conn:
            row = conn.execute('SELECT status,step_count,spec_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None: raise DomainError('TASK_NOT_FOUND', 'Задача не найдена')
            if row['status'] != TaskStatus.RUNNING.value: raise DomainError('INVALID_STATUS', 'Задача не выполняется')
            count = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='action.rejected' AND json_extract(payload_json, '$.reason')='editable_not_focused'", (task_id,)).fetchone()[0]
            if count >= 3: return count
            next_count = row['step_count'] + 1
            if next_count > TaskSpec.model_validate_json(row['spec_json']).limits.max_steps:
                raise DomainError('STEP_LIMIT_EXCEEDED', 'task step limit has been reached')
            self._event(conn, task_id, 'action.rejected', {'code': 'CAPABILITY_UNAVAILABLE', 'reason': 'editable_not_focused'})
            conn.execute('UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?', (next_count, _utc_now(), task_id))
            self._event(conn, task_id, 'task.step_counted', {'step_count': next_count})
            return count + 1

    def record_verbatim_text_rejection(self, task_id: str) -> int:
        """Persist a bounded, payload-free pre-dispatch rejection and spend one step."""
        with self._lock, self._transaction() as conn:
            row = conn.execute('SELECT status,step_count,spec_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise DomainError('TASK_NOT_FOUND', 'task was not found')
            if row['status'] != TaskStatus.RUNNING.value:
                raise DomainError('INVALID_STATUS', 'task is not running')
            count = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='action.rejected' AND json_extract(payload_json, '$.reason')='verbatim_text_mismatch'",
                (task_id,),
            ).fetchone()[0]
            if count >= 3:
                return count
            next_count = row['step_count'] + 1
            if next_count > TaskSpec.model_validate_json(row['spec_json']).limits.max_steps:
                raise DomainError('STEP_LIMIT_EXCEEDED', 'task step limit has been reached')
            self._event(conn, task_id, 'action.rejected', {
                'code': 'VERBATIM_TEXT_MISMATCH', 'reason': 'verbatim_text_mismatch',
            })
            conn.execute('UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?',
                         (next_count, _utc_now(), task_id))
            self._event(conn, task_id, 'task.step_counted', {'step_count': next_count})
            return count + 1

    def record_invalid_key_chord(self, task_id: str) -> int:
        """Persist a payload-free model-output correction and spend one task step."""
        with self._lock, self._transaction() as conn:
            row = conn.execute('SELECT status,step_count,spec_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise DomainError('TASK_NOT_FOUND', 'task was not found')
            if row['status'] != TaskStatus.RUNNING.value:
                raise DomainError('INVALID_STATUS', 'task is not running')
            count = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='model.output_rejected' AND json_extract(payload_json, '$.reason')='invalid_key_chord'",
                (task_id,),
            ).fetchone()[0]
            if count >= 3:
                return count
            next_count = row['step_count'] + 1
            if next_count > TaskSpec.model_validate_json(row['spec_json']).limits.max_steps:
                raise DomainError('STEP_LIMIT_EXCEEDED', 'task step limit has been reached')
            self._event(conn, task_id, 'model.output_rejected', {
                'code': 'MODEL_OUTPUT_INVALID', 'reason': 'invalid_key_chord',
            })
            conn.execute('UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?',
                         (next_count, _utc_now(), task_id))
            self._event(conn, task_id, 'task.step_counted', {'step_count': next_count})
            return count + 1

    def events(self, task_id: str, after_event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after_event_id < 0 or not 1 <= limit <= 1000:
            raise DomainError("INVALID_LIMIT", "after_event_id must be nonnegative and limit must be between 1 and 1000")
        with self._lock:
            if self._closed:
                raise DomainError("REPOSITORY_CLOSED", "task repository is closed")
            if self._conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            rows = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (task_id, after_event_id, limit),
            ).fetchall()
            return [{"event_id": r["event_id"], "task_id": r["task_id"], "event_type": r["event_type"],
                     "timestamp": r["timestamp"], "payload": json.loads(r["payload_json"])} for r in rows]

    def increment_step(self, task_id: str) -> TaskRecord:
        with self._lock, self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            if row["status"] != TaskStatus.RUNNING.value:
                raise DomainError("INVALID_STATUS", "steps can only be counted while a task is running")
            next_count = row["step_count"] + 1
            if next_count > TaskSpec.model_validate_json(row["spec_json"]).limits.max_steps:
                raise DomainError("STEP_LIMIT_EXCEEDED", "task step limit has been reached")
            conn.execute("UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?", (next_count, _utc_now(), task_id))
            self._event(conn, task_id, "task.step_counted", {"step_count": next_count})
            updated = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._record(updated)

    def request_verification_repair(self, task_id: str, failed: list[dict[str, Any]]) -> bool:
        """Atomically spend the task's single repair and one step."""
        with self._lock, self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError("TASK_NOT_FOUND", "task was not found")
            if row["status"] != TaskStatus.RUNNING.value:
                return False
            count = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type=?",
                                 (task_id, "verification.repair_requested")).fetchone()[0]
            if count:
                return False
            next_count = row["step_count"] + 1
            if next_count > TaskSpec.model_validate_json(row["spec_json"]).limits.max_steps:
                raise DomainError("STEP_LIMIT_EXCEEDED", "task step limit has been reached")
            conn.execute("UPDATE tasks SET step_count=?,version=version+1,updated_at=? WHERE task_id=?",
                         (next_count, _utc_now(), task_id))
            self._event(conn, task_id, "task.step_counted", {"step_count": next_count})
            self._event(conn, task_id, "verification.repair_requested", {"failed_checks": failed})
            return True

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
