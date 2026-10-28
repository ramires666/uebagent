"""Opt-in integration tests: RUN_CAMOUFOX_PROFILE_TEST=1 under Xvfb."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import ModuleType

from worker.fingerprint import profile_launch_options


@unittest.skipUnless(os.environ.get("RUN_CAMOUFOX_PROFILE_TEST") == "1", "requires Camoufox and X11")
class FingerprintIntegrationTest(unittest.TestCase):
    def test_restart_and_corruption(self):
        from camoufox.async_api import AsyncCamoufox

        async def run():
            with tempfile.TemporaryDirectory() as root:
                profile = Path(root) / "profile"
                profile.mkdir()
                state = profile.parent / (profile.name + ".camoufox-fingerprint.json")
                snapshots = []
                for _ in range(2):
                    options = profile_launch_options(profile, headless=False, viewport={"width": 1440, "height": 900}, env=dict(os.environ))
                    async with AsyncCamoufox(persistent_context=True, from_options=options) as context:
                        page = context.pages[0] if context.pages else await context.new_page()
                        snapshots.append(await page.evaluate("""() => ({ua: navigator.userAgent, platform: navigator.platform,
                            languages: navigator.languages, width: innerWidth, height: innerHeight,
                            screen: [screen.width, screen.height, screen.colorDepth],
                            canvas: (() => { const c = document.createElement('canvas'); const x = c.getContext('2d');
                            x.fillText('fingerprint test', 5, 20); return c.toDataURL(); })()})"""))
                    if len(snapshots) == 1:
                        cached = json.loads(state.read_text())
                    else:
                        self.assertEqual(cached, json.loads(state.read_text()))
                self.assertEqual(snapshots[0], snapshots[1])
                self.assertEqual((snapshots[0]["width"], snapshots[0]["height"]), (1440, 900))
                state.write_text("{broken", encoding="utf-8")
                with self.assertRaises(ValueError):
                    profile_launch_options(profile, headless=False, viewport={"width": 1440, "height": 900}, env=dict(os.environ))
                self.assertEqual(state.read_text(), "{broken")
                state.unlink()
                with self.assertRaisesRegex(ValueError, "fingerprint missing"):
                    profile_launch_options(profile, headless=False, viewport={"width": 1440, "height": 900}, env=dict(os.environ))
                self.assertFalse(state.exists())
        asyncio.run(run())


@unittest.skipUnless(os.environ.get("RUN_CAMOUFOX_PROFILE_TEST") == "1", "requires Camoufox and X11")
class LegacyMigrationIntegrationTest(unittest.TestCase):
    def test_live_legacy_browser_to_persisted_restart(self):
        import hashlib
        from camoufox.async_api import AsyncCamoufox
        from worker.migrate_fingerprint import migrate

        async def run():
            with tempfile.TemporaryDirectory() as root:
                profile = Path(root) / "profile"
                profile.mkdir()
                viewport = {"width": 1440, "height": 900}
                async with AsyncCamoufox(headless=False, persistent_context=True, user_data_dir=str(profile), viewport=viewport, env=dict(os.environ), os="linux") as context:
                    page = context.pages[0] if context.pages else await context.new_page()
                    expression = """() => ({ua:navigator.userAgent, platform:navigator.platform, languages:navigator.languages, width:innerWidth, height:innerHeight, screen:[screen.width,screen.height,screen.colorDepth], canvas:(()=>{const c=document.createElement('canvas');c.getContext('2d').fillText('legacy migration', 5,20);return c.toDataURL()})(), webgl:(()=>{const gl=document.createElement('canvas').getContext('webgl2');if(!gl)return null;const ext=gl.getExtension('WEBGL_debug_renderer_info');return {vendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null,renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,extensions:gl.getSupportedExtensions()}})()})"""
                    before = await page.evaluate(expression)
                    matches = []
                    for path in Path("/proc").iterdir():
                        if not path.name.isdigit():
                            continue
                        try:
                            args = (path / "cmdline").read_bytes().split(b"\0")
                        except (OSError, ProcessLookupError):
                            continue
                        if args and Path(os.fsdecode(args[0])).name == "camoufox-bin" and b"-profile" in args and os.fsencode(profile) in args:
                            matches.append(int(path.name))
                    self.assertEqual(len(matches), 1)
                    result = migrate(profile, matches[0], hashlib.sha256(before["ua"].encode()).hexdigest(), before["screen"][0], before["screen"][1])
                    self.assertEqual(result["preference_keys"], 2)
                opts = profile_launch_options(profile, headless=False, viewport=viewport, env=dict(os.environ))
                async with AsyncCamoufox(persistent_context=True, from_options=opts) as context:
                    page = context.pages[0] if context.pages else await context.new_page()
                    after = await page.evaluate(expression)
                self.assertEqual(before, after)
                self.assertEqual((after["width"], after["height"]), (1440, 900))
        asyncio.run(run())


class FingerprintStorageTest(unittest.TestCase):
    def test_existing_profile_without_state_fails_before_generation(self):
        calls = []
        fake = ModuleType("camoufox.utils")
        fake.launch_options = lambda **kw: calls.append(kw)
        fake.validate_config = lambda config: None
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"camoufox": ModuleType("camoufox"), "camoufox.utils": fake}), patch("importlib.metadata.version", return_value="0.4.11"):
            profile = Path(root) / "profile"
            profile.mkdir()
            (profile / "existing").write_text("data")
            with self.assertRaisesRegex(ValueError, "fingerprint missing"):
                profile_launch_options(profile, headless=True, viewport={"width": 1440, "height": 900}, env={})
            self.assertEqual(calls, [])
            self.assertFalse((Path(root) / "profile.camoufox-fingerprint.json").exists())

    def test_corrupt_state_fails_before_generation(self):
        calls = []
        fake = ModuleType("camoufox.utils")
        fake.launch_options = lambda **kw: calls.append(kw)
        fake.validate_config = lambda config: None
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"camoufox": ModuleType("camoufox"), "camoufox.utils": fake}), patch("importlib.metadata.version", return_value="0.4.11"):
            profile = Path(root) / "profile"
            state = Path(root) / "profile.camoufox-fingerprint.json"
            state.write_text("{broken")
            if os.name != "nt":
                state.chmod(0o600)
            with self.assertRaises(ValueError):
                profile_launch_options(profile, headless=True, viewport={"width": 1440, "height": 900}, env={})
            self.assertEqual(calls, [])
            self.assertEqual(state.read_text(), "{broken")


class FingerprintMigrationTest(unittest.TestCase):
    def test_migration_source_and_observation_fail_closed(self):
        import hashlib
        from worker.migrate_fingerprint import migrate
        fake = ModuleType("camoufox.utils")
        fake.validate_config = lambda config: None
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"camoufox": ModuleType("camoufox"), "camoufox.utils": fake}), patch("worker.migrate_fingerprint.version", return_value="0.4.11"), patch("os.fchmod", create=True), patch("worker.migrate_fingerprint.infer_legacy_prefs", return_value={"webgl.enable-webgl2": False, "webgl.force-enabled": True}):
            root = Path(root)
            profile = root / "profile"
            profile.mkdir()
            process = root / "proc" / "42"
            process.mkdir(parents=True)
            process.joinpath("cmdline").write_bytes(b"/bin/camoufox-bin\0-profile\0" + os.fsencode(profile) + b"\0")
            config = {"navigator.userAgent": "fixture", "screen.width": 1440, "screen.height": 900, "fonts:spacing_seed": 7, "canvas:aaOffset": 1}
            process.joinpath("environ").write_bytes(b"CAMOU_CONFIG_1=" + json.dumps(config).encode() + b"\0")
            ua_hash = hashlib.sha256(b"fixture").hexdigest()
            state = root / "profile.camoufox-fingerprint.json"
            result = migrate(profile, 42, ua_hash, 1440, 900, proc=root / "proc", dry_run=True)
            self.assertTrue(result['dry_run'])
            self.assertFalse(state.exists())
            self.assertFalse((root / 'profile.camoufox-fingerprint.lock').exists())
            with patch('worker.migrate_fingerprint._check_trusted_source'):
                trusted = migrate(profile, 42, proc=root / 'proc', trusted_live_source=True, dry_run=True)
            self.assertEqual(trusted['sha256'], result['sha256'])
            self.assertEqual(trusted['source'], 'trusted_live_source')
            with patch('worker.migrate_fingerprint._check_trusted_source'):
                with self.assertRaisesRegex(ValueError, 'expected dry-run fingerprint hash'):
                    migrate(profile, 42, proc=root / 'proc', trusted_live_source=True, expected_sha256='0' * 64)
            self.assertFalse(state.exists())
            with patch('worker.migrate_fingerprint._check_trusted_source', side_effect=ValueError('source mismatch')):
                with self.assertRaisesRegex(ValueError, 'source mismatch'):
                    migrate(profile, 42, proc=root / 'proc', trusted_live_source=True, dry_run=True)
            with self.assertRaisesRegex(ValueError, 'profile does not match'):
                other = root / 'other'
                other.mkdir()
                migrate(other, 42, proc=root / 'proc', trusted_live_source=True, dry_run=True)
            with self.assertRaisesRegex(ValueError, "observed user agent"):
                migrate(profile, 42, "0" * 64, 1440, 900, proc=root / "proc")
            self.assertFalse(state.exists())
            result = migrate(profile, 42, ua_hash, 1440, 900, proc=root / "proc")
            self.assertEqual(result["config_keys"], len(config))
            self.assertEqual(json.loads(state.read_text())["config"], config)
            with self.assertRaises(FileExistsError):
                migrate(profile, 42, ua_hash, 1440, 900, proc=root / "proc")

    def test_wrong_source_hash_rejected(self):
        from worker.migrate_fingerprint import _checked_hash
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'source'
            path.write_text('different')
            with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
                _checked_hash(path, '0' * 64)

    def test_ambiguous_webgl_database_fails_closed(self):
        import sqlite3
        from contextlib import closing
        import worker.migrate_fingerprint as migration

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            db_path = root / 'webgl.sqlite'
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('CREATE TABLE webgl_fingerprints (data TEXT, lin INTEGER)')
                db.executemany('INSERT INTO webgl_fingerprints VALUES (?, 1)', [
                    (json.dumps({'webGl2Enabled': True}),),
                    (json.dumps({'webGl2Enabled': False}),),
                ])
                db.commit()
            camoufox = ModuleType('camoufox')
            utils = ModuleType('camoufox.utils')
            utils.__file__ = str(root / 'utils.py')
            utils.get_target_os = lambda config: 'lin'
            async_api = ModuleType('camoufox.async_api')
            async_api.__file__ = str(root / 'async_api.py')
            webgl = ModuleType('camoufox.webgl')
            sample = ModuleType('camoufox.webgl.sample')
            sample.__file__ = str(root / 'sample.py')
            sample.DB_PATH = db_path
            camoufox.utils, camoufox.async_api, camoufox.webgl = utils, async_api, webgl
            webgl.sample = sample
            modules = {'camoufox': camoufox, 'camoufox.utils': utils, 'camoufox.async_api': async_api,
                       'camoufox.webgl': webgl, 'camoufox.webgl.sample': sample}
            with patch.dict('sys.modules', modules), patch.object(migration, '_checked_hash'):
                with self.assertRaisesRegex(ValueError, 'cannot be uniquely inferred'):
                    migration.infer_legacy_prefs({}, root)


class FingerprintLockTest(unittest.TestCase):
    def test_existing_inode_and_concurrent_owner(self):
        from worker.fingerprint import profile_lock
        with tempfile.TemporaryDirectory() as root:
            lock = Path(root) / "lock"
            lock.write_bytes(b"0")
            with profile_lock(lock):
                with self.assertRaises(OSError):
                    with profile_lock(lock):
                        pass
            with profile_lock(lock):
                self.assertTrue(lock.exists())

    def test_lock_released_after_process_killed(self):
        import subprocess
        import sys
        from worker.fingerprint import profile_lock

        with tempfile.TemporaryDirectory() as root:
            lock = Path(root) / "lock"
            script = (
                "import sys,time; from pathlib import Path; "
                "from worker.fingerprint import profile_lock; "
                "guard=profile_lock(Path(sys.argv[1])); guard.__enter__(); "
                "print('locked', flush=True); time.sleep(30)"
            )
            child = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(OSError):
                    with profile_lock(lock):
                        pass
            finally:
                child.kill()
                child.wait(timeout=5)
                child.stdout.close()
            with profile_lock(lock):
                self.assertTrue(lock.exists())
