from __future__ import annotations

import json
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from worker.fingerprint import _SYSTEM_UI_FONT_PREFS, _apply_system_ui_font_preferences, _digest, _prepare_fontconfig, profile_launch_options


READ_BY = {"mac": ["LM", "M"], "win": ["LM", "W"], "lin": ["L", "LM"]}
FONTCONFIG_DIRS = {"mac": "macos", "win": "windows", "lin": "linux"}
SYSTEM_FONTS = {"mac": "Helvetica", "win": "Segoe UI", "lin": "Noto Sans"}


def make_bundle(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "version.json").write_text(json.dumps({"version": "158.0", "release": "beta.40"}), encoding="utf-8")
    (root / "fonts").mkdir(parents=True)
    (root / "fonts" / "groups.json").write_text(json.dumps({"readBy": READ_BY}), encoding="utf-8")
    for group in {group for groups in READ_BY.values() for group in groups} | {"NOT_ALLOWED"}:
        (root / "fonts" / group).mkdir()
    for os_key, directory in FONTCONFIG_DIRS.items():
        target = root / "fontconfig" / directory
        target.mkdir(parents=True)
        if os_key == "mac":
            system_rule = (
                '<match target="pattern"><test qual="any" name="family"><string>system-ui</string></test>'
                '<edit name="family" mode="assign" binding="same"><string>System Font</string></edit></match>'
            )
        elif os_key == "win":
            system_rule = (
                '<match target="pattern"><test qual="any" name="family"><string>system-ui</string></test>'
                '<edit name="family" mode="assign" binding="same"><string>Segoe UI</string></edit></match>'
            )
        else:
            system_rule = '<alias><family>system-ui</family><prefer><family>Noto Sans</family></prefer></alias>'
        (target / "fonts.conf").write_text(
            '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
            '<fontconfig><dir prefix="cwd">fonts</dir>'
            + system_rule +
            '</fontconfig>',
            encoding="utf-8",
        )


@pytest.mark.parametrize(("os_key", "bundle_directory"), [
    ("mac", "macos"), ("win", "windows"), ("lin", "linux"),
])
def test_generates_private_absolute_fontconfig_from_declared_os_groups(tmp_path, os_key, bundle_directory):
    bundle = tmp_path / "camoufox"
    make_bundle(bundle)
    state = tmp_path / "worker-state"
    legacy_path = bundle / "fontconfig" / os_key
    options = {"env": {"FONTCONFIG_PATH": str(legacy_path), "KEEP_ME": "unchanged"}}

    _prepare_fontconfig(options, state)

    env = options["env"]
    config_dir = bundle / "fontconfig" / bundle_directory
    assert env["FONTCONFIG_PATH"] == str(config_dir)
    generated = Path(env["FONTCONFIG_FILE"])
    assert generated.parent == state / "fontconfig"
    content = generated.read_text(encoding="utf-8")
    assert '<dir prefix="cwd">fonts</dir>' not in content
    assert '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">' in content
    assert all(f"<dir>{bundle / 'fonts' / group}</dir>" in content for group in READ_BY[os_key])
    target_tag = "family" if os_key == "lin" else "string"
    assert f"<{target_tag}>{SYSTEM_FONTS[os_key]}</{target_tag}>" in content
    if os_key == "mac":
        assert content.count(f"<string>{SYSTEM_FONTS[os_key]}</string>") == 3
        assert all(f"<string>{alias}</string>" in content for alias in ("System Font", "-apple-system", "BlinkMacSystemFont"))
        assert '<string>system-ui</string></test><edit name="family" mode="assign" binding="same"><string>System Font</string>' in content
    else:
        assert content.count("<string>Helvetica</string>") == 0
        assert "<string>System Font</string>" not in content
    assert str(bundle / "fonts" / "NOT_ALLOWED") not in content
    assert env["KEEP_ME"] == "unchanged"
    assert generated.name.startswith(f"fonts-{os_key}-158.0-beta.40-")
    if os.name != "nt":
        assert generated.stat().st_mode & 0o777 == 0o600
        assert generated.parent.stat().st_mode & 0o777 == 0o700

    before = generated.read_bytes()
    _prepare_fontconfig(options, state)
    assert Path(options["env"]["FONTCONFIG_FILE"]) == generated
    assert generated.read_bytes() == before
    assert len(list((state / "fontconfig").glob("fonts-*.conf"))) == 1


def test_accepts_new_bundle_os_directory_names(tmp_path):
    bundle = tmp_path / "camoufox"
    make_bundle(bundle)
    options = {"env": {"FONTCONFIG_PATH": str(bundle / "fontconfig" / "macos")}}

    _prepare_fontconfig(options, tmp_path / "worker-state")

    assert Path(options["env"]["FONTCONFIG_PATH"]) == bundle / "fontconfig" / "macos"
    assert Path(options["env"]["FONTCONFIG_FILE"]).is_file()


@pytest.mark.parametrize("broken", ["no-groups", "no-os-groups", "missing-group-dir", "bad-template", "external-entity", "missing-system-ui-rule", "missing-version"])
def test_invalid_or_incomplete_bundle_fails_closed(tmp_path, broken):
    bundle = tmp_path / "camoufox"
    make_bundle(bundle)
    if broken == "no-groups":
        (bundle / "fonts" / "groups.json").unlink()
    elif broken == "no-os-groups":
        (bundle / "fonts" / "groups.json").write_text(json.dumps({"readBy": {"win": ["W"]}}), encoding="utf-8")
    elif broken == "missing-group-dir":
        (bundle / "fonts" / "LM").rmdir()
    elif broken == "bad-template":
        (bundle / "fontconfig" / "macos" / "fonts.conf").write_text(
            '<!DOCTYPE fontconfig SYSTEM "https://example.invalid/fonts.dtd"><fontconfig>'
            '<dir prefix="cwd">fonts</dir></fontconfig>', encoding="utf-8")
    elif broken == "external-entity":
        (bundle / "fontconfig" / "macos" / "fonts.conf").write_text(
            '<!DOCTYPE fontconfig SYSTEM "fonts.dtd" [<!ENTITY x SYSTEM "file:///secret">]>'
            '<fontconfig><dir prefix="cwd">fonts</dir></fontconfig>', encoding="utf-8")
    elif broken == "missing-system-ui-rule":
        (bundle / "fontconfig" / "macos" / "fonts.conf").write_text(
            '<fontconfig><dir prefix="cwd">fonts</dir></fontconfig>', encoding="utf-8")
    elif broken == "missing-version":
        (bundle / "version.json").unlink()
    options = {"env": {"FONTCONFIG_PATH": str(bundle / "fontconfig" / "mac")}}

    with pytest.raises(RuntimeError, match="missing|invalid|refusing"):
        _prepare_fontconfig(options, tmp_path / "worker-state")
    assert "FONTCONFIG_FILE" not in options["env"]


def _fake_camoufox(launch_options):
    module = ModuleType("camoufox.utils")
    module.launch_options = launch_options
    module.validate_config = lambda _config: None
    return module


def test_new_profile_launch_prepares_fontconfig_without_persisting_it_in_fingerprint(tmp_path):
    bundle = tmp_path / "camoufox"
    make_bundle(bundle)
    generated = []

    def launch_options(**_kwargs):
        generated.append(True)
        return {"env": {"FONTCONFIG_PATH": str(bundle / "fontconfig" / "mac")}, "firefox_user_prefs": {}}

    profile = tmp_path / "worker-state" / "profiles" / "default"
    profile.mkdir(parents=True)
    with patch.dict("sys.modules", {"camoufox": ModuleType("camoufox"), "camoufox.utils": _fake_camoufox(launch_options)}), \
            patch("importlib.metadata.version", return_value="0.4.11"):
        options = profile_launch_options(profile, headless=True, viewport={"width": 1440, "height": 900}, env={})

    assert generated == [True]
    assert Path(options["env"]["FONTCONFIG_FILE"]).is_file()
    state = profile.parent / "default.camoufox-fingerprint.json"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["config"] == {}
    assert "env" not in saved
    assert saved["firefox_user_prefs"] == {}
    assert saved["sha256"] == _digest(saved["config"], saved["firefox_user_prefs"])
    assert all(options["firefox_user_prefs"][name] == "Arial" for name in _SYSTEM_UI_FONT_PREFS)


def test_reused_profile_launch_prepares_fontconfig_without_changing_fingerprint(tmp_path):
    bundle = tmp_path / "camoufox"
    make_bundle(bundle)
    profile = tmp_path / "worker-state" / "profiles" / "default"
    profile.mkdir(parents=True)
    state = profile.parent / "default.camoufox-fingerprint.json"
    config = {"navigator.userAgent": "Macintosh", "fonts:spacing_seed": 4, "canvas:aaOffset": 0}
    prefs = {"webgl.enable-webgl2": True}
    payload = {"version": "0.4.11", "config": config, "firefox_user_prefs": prefs, "sha256": _digest(config, prefs)}
    state.write_text(json.dumps(payload), encoding="utf-8")
    before = state.read_bytes()
    if os.name != "nt":
        state.chmod(0o600)

    def launch_options(**_kwargs):
        return {"env": {"FONTCONFIG_PATH": str(bundle / "fontconfig" / "mac")}, "firefox_user_prefs": {"webgl.enable-webgl2": True}}

    with patch.dict("sys.modules", {"camoufox": ModuleType("camoufox"), "camoufox.utils": _fake_camoufox(launch_options)}), \
            patch("importlib.metadata.version", return_value="0.4.11"):
        options = profile_launch_options(profile, headless=True, viewport={"width": 1440, "height": 900}, env={})

    assert Path(options["env"]["FONTCONFIG_FILE"]).is_file()
    assert state.read_bytes() == before
    assert options["firefox_user_prefs"]["webgl.enable-webgl2"] is True
    assert all(options["firefox_user_prefs"][name] == "Arial" for name in _SYSTEM_UI_FONT_PREFS)


def test_system_ui_font_preferences_are_added_only_for_macos_os_group(tmp_path):
    options = {
        "env": {"FONTCONFIG_PATH": str(tmp_path / "camoufox" / "fontconfig" / "windows")},
        "firefox_user_prefs": {"webgl.enable-webgl2": True},
    }

    _apply_system_ui_font_preferences(options)

    assert options["firefox_user_prefs"] == {"webgl.enable-webgl2": True}
