"""Persist the Camoufox-generated profile configuration, never browser credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

VERSION = "0.4.11"
MAX_BYTES = 256 * 1024
_FONTCONFIG_DIRS = {"mac": "macos", "win": "windows", "lin": "linux"}
_FONTCONFIG_KEYS = {"macos": "mac", "windows": "win", "linux": "lin"}
_FONTCONFIG_PLACEHOLDER = '<dir prefix="cwd">fonts</dir>'
_FONTCONFIG_SYSTEM_UI_TO_SYSTEM_FONT_RULE = re.compile(
    r'<match target="pattern">\s*'
    r'<test qual="any" name="family">\s*<string>system-ui</string>\s*</test>\s*'
    r'<edit name="family" mode="assign" binding="same">\s*<string>System Font</string>\s*</edit>\s*'
    r'</match>',
    re.IGNORECASE,
)
_FONTCONFIG_DOCTYPE = re.compile(
    r'<!DOCTYPE\s+fontconfig\s+SYSTEM\s+(?P<quote>["\'])(?:fonts\.dtd|urn:fontconfig:fonts\.dtd)(?P=quote)\s*>',
    re.IGNORECASE,
)
_DOCTYPE_TOKEN = re.compile(r"<!DOCTYPE\b.*?>", re.IGNORECASE | re.DOTALL)
_SYSTEM_UI_FONT_PREFS = {
    "font.name.system-ui.x-western": "Arial",
    "font.name-list.system-ui.x-western": "Arial",
    "font.name.system-ui.x-unicode": "Arial",
    "font.name-list.system-ui.x-unicode": "Arial",
}


def readiness_default_addons_enabled() -> bool:
    """Test-only variant switch; production always keeps Camoufox defaults."""
    test_trace = (os.environ.get("APP_ENV", "production").lower() == "test"
                  and os.environ.get("READINESS_ACTION_TRACE") == "1")
    return not (test_trace and os.environ.get("READINESS_DISABLE_DEFAULT_ADDONS") == "1")


def _prepare_fontconfig(options: dict[str, Any], state_dir: Path) -> None:
    """Build a private absolute-path fontconfig for the selected Camoufox OS."""
    env = options.get("env")
    if not isinstance(env, dict):
        raise RuntimeError("Camoufox launch options are missing their environment map")
    raw_path = env.get("FONTCONFIG_PATH")
    if raw_path is None:
        if os.name != "nt":
            raise RuntimeError("Camoufox FONTCONFIG_PATH is missing; verify the Python launcher and browser font bundle versions")
        return
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Camoufox FONTCONFIG_PATH is invalid; verify the browser font bundle")

    path = Path(raw_path)
    fontconfig_root = path.parent
    if fontconfig_root.name != "fontconfig":
        raise RuntimeError("Camoufox FONTCONFIG_PATH is outside the expected browser bundle")
    bundle_root = fontconfig_root.parent
    os_key = path.name if path.name in _FONTCONFIG_DIRS else _FONTCONFIG_KEYS.get(path.name)
    bundle_os_name = _FONTCONFIG_DIRS.get(os_key or "")
    if bundle_os_name is None:
        raise RuntimeError("Camoufox FONTCONFIG_PATH has an unsupported OS directory")
    bundle_config_dir = fontconfig_root / bundle_os_name
    template_path = bundle_config_dir / "fonts.conf"
    fonts_root = bundle_root / "fonts"
    groups_path = fonts_root / "groups.json"
    version_path = bundle_root / "version.json"
    if (not bundle_config_dir.is_dir() or not template_path.is_file() or template_path.is_symlink()
            or not fonts_root.is_dir() or not groups_path.is_file() or not version_path.is_file()):
        raise RuntimeError("Camoufox fontconfig or bundled fonts are missing; verify the browser bundle")

    try:
        groups_data = json.loads(groups_path.read_text(encoding="utf-8"))
        read_by = groups_data.get("readBy") if isinstance(groups_data, dict) else None
        selected_groups = read_by.get(os_key) if isinstance(read_by, dict) else None
        if (not isinstance(selected_groups, list) or not selected_groups
                or any(not isinstance(group, str) or not re.fullmatch(r"[A-Z]{1,4}", group)
                       for group in selected_groups)):
            raise ValueError("font groups are missing for the selected OS")
        font_dirs: list[Path] = []
        fonts_root_resolved = fonts_root.resolve(strict=True)
        for group in selected_groups:
            font_dir = fonts_root / group
            resolved_dir = font_dir.resolve(strict=True)
            if not resolved_dir.is_dir() or not resolved_dir.is_relative_to(fonts_root_resolved):
                raise ValueError("font group directory is missing or outside the bundle")
            font_dirs.append(resolved_dir)

        version_data = json.loads(version_path.read_text(encoding="utf-8"))
        bundle_version = version_data.get("version") if isinstance(version_data, dict) else None
        bundle_release = version_data.get("release") if isinstance(version_data, dict) else None
        if (not isinstance(bundle_version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", bundle_version)
                or not isinstance(bundle_release, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", bundle_release)):
            raise ValueError("browser bundle version metadata is invalid")

        template = template_path.read_text(encoding="utf-8")
        doctype_tokens = list(_DOCTYPE_TOKEN.finditer(template))
        if ("<!ENTITY" in template.upper() or len(doctype_tokens) > 1
                or template.count(_FONTCONFIG_PLACEHOLDER) != 1):
            raise ValueError("fontconfig template is malformed or unsupported")
        validation_template = template
        if doctype_tokens:
            doctype = doctype_tokens[0]
            if _FONTCONFIG_DOCTYPE.fullmatch(doctype.group(0)) is None:
                raise ValueError("fontconfig template has an unsupported external DTD")
            # ElementTree does not fetch external DTDs; strip the one known
            # Fontconfig declaration for syntax validation, but preserve it in
            # the generated file consumed by Fontconfig.
            validation_template = template[:doctype.start()] + template[doctype.end():]
        replacement = "\n\t".join(f"<dir>{escape(str(directory))}</dir>" for directory in font_dirs)
        content = template.replace(_FONTCONFIG_PLACEHOLDER, replacement)
        validation_content = validation_template.replace(_FONTCONFIG_PLACEHOLDER, replacement)
        if os_key == "mac":
            if len(_FONTCONFIG_SYSTEM_UI_TO_SYSTEM_FONT_RULE.findall(content)) != 1:
                raise ValueError("macOS fontconfig template has no unique system-ui family rule")
            system_font_rules = "\n\t".join(
                "<match target=\"pattern\">\n"
                "\t\t<test qual=\"any\" name=\"family\"><string>" + alias + "</string></test>\n"
                "\t\t<edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Helvetica</string></edit>\n"
                "\t</match>"
                for alias in ("System Font", "-apple-system", "BlinkMacSystemFont")
            )
            content = content.replace("</fontconfig>", system_font_rules + "\n</fontconfig>")
            validation_content = validation_content.replace("</fontconfig>", system_font_rules + "\n</fontconfig>")
        ElementTree.fromstring(validation_content)
    except (OSError, UnicodeError, json.JSONDecodeError, ElementTree.ParseError, ValueError) as exc:
        raise RuntimeError("Camoufox bundled font metadata is invalid; refusing system-font fallback") from exc

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    generated_dir = state_dir / "fontconfig"
    generated_path = generated_dir / f"fonts-{os_key}-{bundle_version}-{bundle_release}-{content_hash}.conf"
    try:
        generated_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = generated_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("runtime fontconfig directory is not a private directory")
        if os.name != "nt":
            os.chmod(generated_dir, 0o700)
        if generated_path.exists() or generated_path.is_symlink():
            info = generated_path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError("runtime fontconfig file is not regular")
            if generated_path.read_text(encoding="utf-8") != content:
                raise ValueError("runtime fontconfig content hash mismatch")
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValueError("runtime fontconfig file permissions are too broad")
        else:
            temp_fd, temp_name = tempfile.mkstemp(prefix=".fonts-", dir=generated_dir)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(temp_fd, 0o600)
                with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_name, generated_path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Could not create a private Camoufox fontconfig; refusing system-font fallback") from exc

    # FONTCONFIG_FILE is authoritative; retain FONTCONFIG_PATH at the matching
    # bundle directory for callers which also inspect the legacy variable.
    env["FONTCONFIG_PATH"] = str(bundle_config_dir)
    env["FONTCONFIG_FILE"] = str(generated_path)


def _apply_system_ui_font_preferences(options: dict[str, Any]) -> None:
    """Prefer the readable macOS persona fallback before Gecko's native UI font."""
    env = options.get("env")
    fontconfig_path = env.get("FONTCONFIG_PATH") if isinstance(env, dict) else None
    if not isinstance(fontconfig_path, str) or Path(fontconfig_path).name.lower() not in {"mac", "macos"}:
        return
    prefs = options.get("firefox_user_prefs")
    if not isinstance(prefs, dict):
        raise RuntimeError("Camoufox launch options are missing their Firefox preferences")
    effective_prefs = dict(prefs)
    effective_prefs.update(_SYSTEM_UI_FONT_PREFS)
    options["firefox_user_prefs"] = effective_prefs


def _digest(config: dict[str, Any], prefs: dict[str, Any]) -> str:
    canonical = json.dumps({"config": config, "firefox_user_prefs": prefs}, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def profile_lock(path: Path):
    """Nonblocking OS lock; the persistent inode survives crashes and unlocks on exit."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid fingerprint lock")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def profile_launch_options(profile: Path, *, headless: bool, viewport: dict[str, int], env: dict[str, str],
                          readiness_config: dict[str, bool] | None = None) -> dict[str, Any]:
    from importlib.metadata import version
    from camoufox.utils import launch_options, validate_config

    addon_options: dict[str, Any] = {}
    if not readiness_default_addons_enabled():
        from camoufox import DefaultAddons
        addon_options["exclude_addons"] = [DefaultAddons.UBO]

    if version("camoufox") != VERSION:
        raise RuntimeError("unsupported Camoufox version for persisted fingerprint")
    state = profile.parent / (profile.name + ".camoufox-fingerprint.json")
    lock = profile.parent / (profile.name + ".camoufox-fingerprint.lock")
    profile.parent.mkdir(parents=True, exist_ok=True)
    # A second worker must fail closed rather than race fingerprint creation.
    with profile_lock(lock):
        if state.exists() or state.is_symlink():
            info = state.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("invalid fingerprint file")
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValueError("fingerprint file permissions are too broad")
            obj = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(obj, dict) or set(obj) != {"version", "config", "firefox_user_prefs", "sha256"} or obj["version"] != VERSION:
                raise ValueError("invalid fingerprint schema or version")
            config, prefs = obj["config"], obj["firefox_user_prefs"]
            if not isinstance(config, dict) or not isinstance(prefs, dict):
                raise ValueError("invalid fingerprint data")
            if not isinstance(obj["sha256"], str) or not hmac.compare_digest(obj["sha256"], _digest(config, prefs)):
                raise ValueError("fingerprint integrity check failed")
            if not {"navigator.userAgent", "fonts:spacing_seed", "canvas:aaOffset"}.issubset(config):
                raise ValueError("incomplete fingerprint data")
            if set(prefs) - {"webgl.enable-webgl2", "webgl.force-enabled"} or any(type(v) is not bool for v in prefs.values()):
                raise ValueError("invalid persisted browser preferences")
            validate_config(config)
            effective_config = dict(config)
            if addon_options:
                # Fingerprint files are shared across variants; remove only the
                # known Camoufox addon list from this launch's working copy.
                effective_config.pop("addons", None)
            options = launch_options(config=effective_config, firefox_user_prefs=dict(prefs), headless=headless,
                                     user_data_dir=str(profile), viewport=viewport, env=env, geoip=False,
                                     i_know_what_im_doing=True, **addon_options)
            if readiness_config is not None:
                readiness_config["default_addons_enabled"] = bool(effective_config.get("addons"))
            _apply_system_ui_font_preferences(options)
            _prepare_fontconfig(options, profile.parent.parent)
            return options
        # An existing browser profile without its fingerprint must not silently
        # acquire a new identity (for example after accidental state deletion).
        if profile.exists() and any(profile.iterdir()):
            raise ValueError("fingerprint missing for existing browser profile")
        config: dict[str, Any] = {}
        options = launch_options(config=config, headless=headless, user_data_dir=str(profile), viewport=viewport,
                                 env=env, geoip=False, **addon_options)
        if readiness_config is not None:
            readiness_config["default_addons_enabled"] = bool(config.get("addons"))
        persisted_prefs = options.get("firefox_user_prefs")
        if not isinstance(persisted_prefs, dict):
            raise RuntimeError("Camoufox launch options are missing their Firefox preferences")
        persisted_prefs = dict(persisted_prefs)
        _apply_system_ui_font_preferences(options)
        _prepare_fontconfig(options, profile.parent.parent)
        obj = {"version": VERSION, "config": config, "firefox_user_prefs": persisted_prefs,
               "sha256": _digest(config, persisted_prefs)}
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("fingerprint file too large")
        temp_fd, temp_name = tempfile.mkstemp(prefix=".fingerprint-", dir=profile.parent)
        try:
            os.fchmod(temp_fd, 0o600) if hasattr(os, "fchmod") else None
            with os.fdopen(temp_fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, state)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return options
