"""Create or verify a SQLite checkpoint inside its existing persistent volume."""
from __future__ import annotations

import argparse, hashlib, json, os, re, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOTS = {
    "control": ("BROWSER_MCP_DATA_DIR", "/app/runtime/control-data", "tasks.sqlite3"),
    "worker": ("WORKER_DATA_DIR", "/app/runtime/worker-state", "worker.sqlite3"),
}
ORIGIN = "http://localhost:8090"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TABLES = {"control": {"tasks", "task_events", "task_capabilities"},
          "worker": {"action_ledger", "cookie_imports", "capability_uses"}}


def stop(code: str) -> None:
    raise RuntimeError(code)


def source_path(role: str) -> Path:
    env_key, root_text, filename = ROOTS[role]
    if os.environ.get(env_key) != root_text or os.environ.get("APP_ENV", "production").lower() in {"test", "debug"}:
        stop("identity_or_test_environment_rejected")
    if role == "control" and os.environ.get("BROWSER_MCP_ORIGIN") != ORIGIN:
        stop("control_origin_rejected")
    if role == "worker" and (os.environ.get("PROXY_ASSIGNMENT_ID") != "direct-development" or
        os.environ.get("ROUTE_GENERATION") != "1" or os.environ.get("EGRESS_READY", "").lower() != "true"):
        stop("worker_route_metadata_rejected")
    root, source = Path(root_text), Path(root_text) / filename
    if root.is_symlink() or not root.is_dir() or source.is_symlink() or not source.is_file():
        stop("fixed_database_path_invalid")
    if source.resolve(strict=True) != source:
        stop("fixed_database_path_resolution_failed")
    return source


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)


def summary(path: Path, role: str) -> dict:
    with closing(connect_ro(path)) as db:
        db.execute("BEGIN")
        if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
            stop("integrity_check_failed")
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not TABLES[role].issubset(names):
            stop("expected_schema_tables_missing")
        counts = {n: int(db.execute('SELECT COUNT(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0]) for n in names}
        result = {"integrity_check": "ok", "table_counts": counts}
        if role == "control":
            result["task_status_counts"] = dict((str(s), int(n)) for s, n in db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status ORDER BY status"))
            digest = hashlib.sha256()
            for task_id, status in db.execute("SELECT task_id,status FROM tasks ORDER BY task_id"):
                digest.update(str(task_id).encode()); digest.update(b"\0"); digest.update(str(status).encode()); digest.update(b"\n")
            result["task_id_status_sha256"] = digest.hexdigest()
            result["task_count"] = counts["tasks"]
        return result


def folder(root: Path, checkpoint_id: str, create: bool) -> Path:
    if not ID_RE.fullmatch(checkpoint_id):
        stop("checkpoint_id_invalid")
    parent = root / "remediation-backups"
    if parent.is_symlink():
        stop("checkpoint_parent_is_symlink")
    if create:
        parent.mkdir(mode=0o700, exist_ok=True); os.chmod(parent, 0o700)
        target = parent / checkpoint_id
        target.mkdir(mode=0o700, exist_ok=False); os.chmod(target, 0o700)
    else:
        target = parent / checkpoint_id
        if target.is_symlink() or not target.is_dir():
            stop("checkpoint_not_found_or_symlink")
    if target.resolve(strict=True).parent != parent.resolve(strict=True):
        stop("checkpoint_path_escape")
    return target


def snapshot(source: Path, backup: Path) -> None:
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    os.chmod(backup, 0o600)
    with closing(connect_ro(source)) as src, closing(sqlite3.connect(backup, timeout=10)) as dst:
        src.backup(dst, pages=256, sleep=0.05)
    os.chmod(backup, 0o600)


def run(role: str, checkpoint_id: str, verify_only: bool) -> dict:
    source = source_path(role)
    before = summary(source, role)
    if verify_only:
        target = folder(source.parent, checkpoint_id, False)
        backup, manifest = target / (source.name + ".checkpoint"), target / "metadata.json"
        if backup.is_symlink() or manifest.is_symlink() or not backup.is_file() or not manifest.is_file():
            stop("checkpoint_artifacts_invalid")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("status") != "CREATED" or metadata.get("role") != role or metadata.get("checkpoint_id") != checkpoint_id:
            stop("checkpoint_metadata_mismatch")
        after = summary(backup, role)
        return {"status": "VERIFIED" if before == after else "MISMATCH", "role": role,
                "checkpoint_id": checkpoint_id, "checkpoint_path": str(backup), "summary_matches": before == after,
                "source_summary": before, "checkpoint_integrity": after["integrity_check"]}
    target = folder(source.parent, checkpoint_id, True)
    backup = target / (source.name + ".checkpoint")
    snapshot(source, backup)
    after = summary(backup, role)
    result = {"status": "CREATED" if before == after else "MISMATCH", "role": role,
              "checkpoint_id": checkpoint_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "checkpoint_path": str(backup), "database_file": backup.name,
              "file_mode_octal": "0600", "directory_mode_octal": "0700",
              "summary_matches_at_creation": before == after, "source_summary": before}
    fd = os.open(target / "metadata.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True); f.write("\n")
    os.chmod(target / "metadata.json", 0o600)
    if before != after:
        stop("checkpoint_summary_mismatch")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=tuple(ROOTS), required=True)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.role, args.checkpoint_id, args.verify_only)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] in {"CREATED", "VERIFIED"} else 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(json.dumps({"status": "ERROR", "code": code[:80]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
