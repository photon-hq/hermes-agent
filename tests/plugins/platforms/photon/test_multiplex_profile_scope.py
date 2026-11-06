"""Multiplex secondary-profile scope for the Photon adapter and its credential reads.

Under ``gateway.multiplex_profiles``, ``os.environ`` holds the DEFAULT profile's YAML-to-env bridge output.
Every Photon setting is read through ``get_scoped_secret``, so a secondary profile with its own (or no)
Photon config never authenticates against the default profile's project or inherits its gating
and formatting switches. Mirrors the LINE/DingTalk/IRC/Mattermost fix for #98738.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon import auth as photon_auth
from plugins.platforms.photon.adapter import PhotonAdapter

_PHOTON_ENV = (
    "PHOTON_PROJECT_ID", "PHOTON_API_KEY", "PHOTON_PROJECT_SECRET", "PHOTON_REQUIRE_MENTION",
    "PHOTON_MARKDOWN", "PHOTON_READ_RECEIPTS", "PHOTON_HOME_CHANNEL", "PHOTON_HOME_CHANNEL_NAME", "PHOTON_API_HOST",
)


@pytest.fixture
def tmp_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate from a real auth.json, which load_runtime_credentials() falls back to."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in _PHOTON_ENV:
        monkeypatch.delenv(key, raising=False)
    yield home
    for key in _PHOTON_ENV:
        os.environ.pop(key, None)


@pytest.fixture
def multiplex_scope():
    """Install multiplex + a secondary-profile secret scope; restore after."""
    tokens = []

    def install(scope=None):
        from agent.secret_scope import set_multiplex_active, set_secret_scope

        set_multiplex_active(True)
        tokens.append(set_secret_scope(scope or {}))
        return tokens[-1]

    yield install

    from agent.secret_scope import reset_secret_scope, set_multiplex_active

    for token in reversed(tokens):
        reset_secret_scope(token)
    set_multiplex_active(False)


@pytest.fixture
def default_profile_env(monkeypatch):
    """The default profile's YAML-to-env bridge output in os.environ."""
    monkeypatch.setenv("PHOTON_PROJECT_ID", "pho_prj_default")
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_default")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "default-spectrum-secret")
    monkeypatch.setenv("PHOTON_REQUIRE_MENTION", "true")
    monkeypatch.setenv("PHOTON_MARKDOWN", "false")
    monkeypatch.setenv("PHOTON_READ_RECEIPTS", "false")
    monkeypatch.setenv("PHOTON_HOME_CHANNEL", "+15551234567")
    monkeypatch.setenv("PHOTON_API_HOST", "https://default-profile.invalid")


def test_scoped_miss_does_not_borrow_the_default_profile(tmp_hermes_home, multiplex_scope, default_profile_env):
    multiplex_scope({"SOMETHING_ELSE": "x"})
    cfg = PlatformConfig(enabled=True, extra={})

    adapter = PhotonAdapter(cfg)

    assert photon_auth.load_runtime_credentials() == (None, None)
    assert (adapter._project_id, adapter._project_api_key) == ("", "")
    assert photon_adapter.validate_config(cfg) is False
    assert photon_adapter._env_enablement() is None
    assert photon_auth.has_legacy_credentials() is False
    assert adapter.require_mention is False
    assert photon_adapter._markdown_enabled() is True
    assert photon_adapter._env_flag("PHOTON_READ_RECEIPTS", "true") is True
    assert photon_auth._api_host() == photon_auth.DEFAULT_API_HOST


def test_secondary_profile_runs_on_its_own_credentials(tmp_hermes_home, multiplex_scope, default_profile_env):
    multiplex_scope({"PHOTON_PROJECT_ID": "pho_prj_profile", "PHOTON_API_KEY": "pho_sk_profile",
                     "PHOTON_API_HOST": "https://profile.invalid/"})

    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra={}))

    assert (adapter._project_id, adapter._project_api_key) == ("pho_prj_profile", "pho_sk_profile")
    assert photon_auth._api_host() == "https://profile.invalid"
    assert photon_adapter._env_enablement()["project_id"] == "pho_prj_profile"


def test_secondary_extra_supplies_the_project_id_its_scope_lacks(tmp_hermes_home, multiplex_scope, default_profile_env):
    multiplex_scope({"PHOTON_API_KEY": "pho_sk_profile"})

    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra={"project_id": "pho_prj_profile"}))

    assert (adapter._project_id, adapter._project_api_key) == ("pho_prj_profile", "pho_sk_profile")
