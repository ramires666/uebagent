"""Explicit, fail-closed migration from a live Camoufox process; never reads cookies."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import tempfile
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

from worker.fingerprint import MAX_BYTES, VERSION, _digest, profile_lock


LEGACY_WORKER_SHA256 = "235a0b8a7907a05206f38055c715442881a323a7bf455ebf9e26dc8da95c3b81"
CAMOUFOX_UTILS_SHA256 = "4cc82c16d9b9987617b38648bd6d3baa0350396ec2426024c5415ef7844dfefa"
CAMOUFOX_SAMPLE_SHA256 = "374722f68f9f7ce07a61be724fd947ed781e8d3e99e9a323dbe263095bc62e60"
CAMOUFOX_ASYNC_SHA256 = "051abb5f564d6bcd1101b769aeeb56d53605830721982b2f45081bb38a484c7f"
WEBGL_DB_SHA256 = "d293cabe7b546bf4ac3a2fb6c3cb6aee9891910659299d61fa2c88944d38c45d"
LEGACY_CONTAINER_PREFIX = "9966c143d27b"
CAMOUFOX_BIN_SHA256 = "171f3b0f375873175d1c68b3acca7feb1f9ccc0a6e2b7b70946287a29aabef80"
CAMOUFOX_OMNI_SHA256 = "c7b32fdb374419cccac0999cc3d2c0fe63e1d987eac9aec262afe5a0284ea6f5"
CAMOUFOX_BROWSER_OMNI_SHA256 = "a660abdc7efec9cc2d34dc5667d1c55106cfcac3a8433e364bb007b054b9e93b"


def _checked_hash(path: Path, expected: str) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("legacy launch provenance mismatch")


def infer_legacy_prefs(config: dict, profile: Path) -> dict:
    # The exact old worker invokes AsyncCamoufox without custom config/prefs.
    # Its pinned Camoufox launch_options takes webGl2Enabled from an OS-filtered
    # database row and sets webgl.force-enabled=True. We infer only when every
    # eligible row agrees; no random sampling and no guessed browser defaults.
    import camoufox.utils as utils
    import camoufox.async_api as async_api
    import camoufox.webgl.sample as sample
    _checked_hash(Path(__file__).with_name("app.py"), LEGACY_WORKER_SHA256)
    _checked_hash(Path(utils.__file__), CAMOUFOX_UTILS_SHA256)
    _checked_hash(Path(async_api.__file__), CAMOUFOX_ASYNC_SHA256)
    _checked_hash(Path(sample.__file__), CAMOUFOX_SAMPLE_SHA256)
    _checked_hash(Path(sample.DB_PATH), WEBGL_DB_SHA256)
    if (profile / "user.js").exists() or (profile / "user.js").is_symlink():
        raise ValueError("custom user.js prevents safe inference")
    target = utils.get_target_os(config)
    if target not in ("win", "mac", "lin"):
        raise ValueError("unknown fingerprint operating system")
    with closing(sqlite3.connect(f"file:{sample.DB_PATH}?mode=ro", uri=True)) as db:
        rows = db.execute(f"SELECT data FROM webgl_fingerprints WHERE {target} > 0").fetchall()
    values = [json.loads(row[0]).get("webGl2Enabled") for row in rows]
    if not values or any(type(value) is not bool for value in values) or len(set(values)) != 1:
        raise ValueError("WebGL2 preference cannot be uniquely inferred")
    return {"webgl.enable-webgl2": values[0], "webgl.force-enabled": True}


def _check_trusted_source(process: Path, args: list[bytes], proc: Path) -> None:
    if not Path('/.dockerenv').is_file() or socket.gethostname() != LEGACY_CONTAINER_PREFIX:
        raise ValueError('unexpected container identity')
    binary = Path(os.fsdecode(args[0])).resolve(strict=True)
    if (process / 'exe').resolve(strict=True) != binary:
        raise ValueError('running executable does not match browser binary')
    _checked_hash(binary, CAMOUFOX_BIN_SHA256)
    _checked_hash(binary.parent / 'omni.ja', CAMOUFOX_OMNI_SHA256)
    _checked_hash(binary.parent / 'browser' / 'omni.ja', CAMOUFOX_BROWSER_OMNI_SHA256)
    current = process
    for _ in range(12):
        status = (current / 'status').read_text(encoding='ascii')
        match = re.search(r'^PPid:\s+(\d+)$', status, re.MULTILINE)
        if not match or int(match.group(1)) <= 1:
            break
        current = proc / match.group(1)
        parent_args = (current / 'cmdline').read_bytes().split(b'\0')
        if len(parent_args) >= 3 and Path(os.fsdecode(parent_args[0])).name.startswith('python') and parent_args[1:3] == [b'-m', b'worker.app']:
            return
    raise ValueError('browser is not descended from the expected worker')


def migrate(profile: Path, pid: int, ua_sha256: str | None = None, screen_width: int | None = None, screen_height: int | None = None, *, proc: Path = Path('/proc'), trusted_live_source: bool = False, dry_run: bool = False, expected_sha256: str | None = None) -> dict:
    from camoufox.utils import validate_config

    if version('camoufox') != VERSION:
        raise ValueError('unsupported Camoufox version')
    profile = profile.resolve(strict=True)
    process = proc / str(pid)
    args = (process / 'cmdline').read_bytes().split(b'\0')
    if not args or Path(os.fsdecode(args[0])).name != 'camoufox-bin' or b'-contentproc' in args:
        raise ValueError('PID is not a Camoufox browser process')
    if args.count(b'-profile') != 1 or args.index(b'-profile') + 1 >= len(args):
        raise ValueError('browser profile argument missing')
    if Path(os.fsdecode(args[args.index(b'-profile') + 1])).resolve() != profile:
        raise ValueError('PID profile does not match requested profile')
    if trusted_live_source:
        if any(value is not None for value in (ua_sha256, screen_width, screen_height)):
            raise ValueError('trusted live source and observed arguments are mutually exclusive')
        _check_trusted_source(process, args, proc)
    elif any(value is None for value in (ua_sha256, screen_width, screen_height)):
        raise ValueError('independently observed user agent and screen are required')
    chunks = {}
    for entry in (process / 'environ').read_bytes().split(b'\0'):
        key, sep, value = entry.partition(b'=')
        match = re.fullmatch(rb'CAMOU_CONFIG_([1-9][0-9]*)', key)
        if match and sep:
            index = int(match.group(1))
            if index in chunks:
                raise ValueError('duplicate config chunk')
            chunks[index] = value
    if not chunks or sorted(chunks) != list(range(1, len(chunks) + 1)) or sum(map(len, chunks.values())) > MAX_BYTES:
        raise ValueError('missing or invalid config chunks')
    config = json.loads(b''.join(chunks[i] for i in sorted(chunks)))
    if not isinstance(config, dict) or not {'navigator.userAgent', 'screen.width', 'screen.height', 'fonts:spacing_seed', 'canvas:aaOffset'}.issubset(config):
        raise ValueError('incomplete browser config')
    validate_config(config)
    ua = config['navigator.userAgent']
    if not isinstance(ua, str) or (not trusted_live_source and hashlib.sha256(ua.encode()).hexdigest() != ua_sha256.lower()):
        raise ValueError('observed user agent does not match')
    if not trusted_live_source and (config['screen.width'], config['screen.height']) != (screen_width, screen_height):
        raise ValueError('observed screen does not match')
    prefs = infer_legacy_prefs(config, profile)
    state = profile.parent / (profile.name + '.camoufox-fingerprint.json')
    lock = profile.parent / (profile.name + '.camoufox-fingerprint.lock')
    obj = {'version': VERSION, 'config': config, 'firefox_user_prefs': prefs, 'sha256': _digest(config, prefs)}
    data = json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('fingerprint too large')
    if trusted_live_source and not dry_run and (not expected_sha256 or not hmac.compare_digest(obj['sha256'], expected_sha256.lower())):
        raise ValueError('expected dry-run fingerprint hash is missing or does not match')
    result = {'sha256': obj['sha256'], 'config_keys': len(config), 'preference_keys': len(prefs),
              'webgl2': prefs['webgl.enable-webgl2'], 'webgl_force_enabled': prefs['webgl.force-enabled'],
              'source': 'trusted_live_source' if trusted_live_source else 'independent_observation', 'dry_run': dry_run}
    if dry_run:
        if state.exists() or state.is_symlink():
            raise FileExistsError('fingerprint state already exists')
        return result
    with profile_lock(lock):
        fd, tmp = tempfile.mkstemp(prefix='.fingerprint-migration-', dir=profile.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Hard link is atomic and refuses overwrite, including a symlink.
            os.link(tmp, state)
        finally:
            os.unlink(tmp)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--observed-user-agent-sha256')
    parser.add_argument('--observed-screen-width', type=int)
    parser.add_argument('--observed-screen-height', type=int)
    parser.add_argument('--trusted-live-source', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--expected-sha256', help='required for trusted-live-source writes; use the reviewed dry-run hash')
    args = parser.parse_args()
    result = migrate(args.profile, args.pid, args.observed_user_agent_sha256, args.observed_screen_width, args.observed_screen_height, trusted_live_source=args.trusted_live_source, dry_run=args.dry_run, expected_sha256=args.expected_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
