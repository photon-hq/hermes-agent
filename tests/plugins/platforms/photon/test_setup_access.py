"""`hermes photon setup` end to end against an in-process Photon 2.0 API (see conftest.py).

Setup must reuse project credentials that Photon still accepts without any browser sign-in,
replace a refused key (revoking the old one), provision a new project for a first run, move
Photon Spectrum credentials aside, and never report routing live before the operator has
texted their shared line. `_autoconfigure_access` fills only unset keys, so a re-run never
clobbers a hand-tuned allowlist.
"""
from __future__ import annotations

import argparse

import pytest

from hermes_cli.config import get_env_value, save_env_value
from plugins.platforms.photon import auth as photon_auth
from plugins.platforms.photon import cli
from plugins.platforms.photon.adapter import _env_enablement

PROJECT_ID = "pho_prj_00000000000000000000000001"
PHONE = "+15551234567"
LINE = "+16282679185"
_PROJECT = f"/v1/projects/{PROJECT_ID}"
_ASSIGNMENTS = f"{_PROJECT}/platforms/imessage/assignments"


def _args(**overrides) -> argparse.Namespace:
    values = {"project_name": None, "phone": PHONE, "first_name": None, "last_name": None, "email": None,
              "no_browser": True}
    return argparse.Namespace(**{**values, **overrides})


@pytest.fixture
def non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)

    def _no_browser_login(_args):
        raise AssertionError("setup must not start a device login here")
    monkeypatch.setattr(cli, "_device_login", _no_browser_login)


def _signed_in() -> None:
    photon_auth._store_pool_record("photon", {
        "access_token": "acc-session", "refresh_token": "ref-session", "expires_at": 4102444800})


def _line_routes(photon_api, *, texted: bool) -> None:
    photon_api.on("GET", f"{_PROJECT}/platforms/imessage", photon_api.shared_mode())
    photon_api.on("GET", _ASSIGNMENTS, {"assignments": [photon_api.assignment(PHONE, LINE)]})
    photon_api.on("GET", f"{_PROJECT}/messages", {"items": [photon_api.received_text(PHONE)] if texted else []})


def test_autoconfigure_access_fills_unset(photon_home) -> None:
    cli._autoconfigure_access(PHONE)

    assert get_env_value("PHOTON_ALLOWED_USERS") == PHONE
    assert get_env_value("PHOTON_HOME_CHANNEL") == PHONE


@pytest.mark.parametrize("home, kept", [
    ("pho_spc_00000000000000000000000001", True),
    ("+15559876543", True),
    ("any;-;+15559876543", False),
    ("iMessage;+;chat527148912345", False),
], ids=["space-id", "phone", "spectrum-dm-guid", "spectrum-group-guid"])
def test_autoconfigure_access_replaces_only_a_spectrum_home_channel(photon_home, home, kept) -> None:
    save_env_value("PHOTON_ALLOWED_USERS", "+15559876543")
    save_env_value("PHOTON_HOME_CHANNEL", home)

    cli._autoconfigure_access(PHONE)

    assert get_env_value("PHOTON_ALLOWED_USERS") == "+15559876543"
    assert get_env_value("PHOTON_HOME_CHANNEL") == (home if kept else PHONE)


def test_env_enablement_seeds_home_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTON_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_test")
    monkeypatch.setenv("PHOTON_HOME_CHANNEL", PHONE)
    monkeypatch.setenv("PHOTON_HOME_CHANNEL_NAME", "Primary DM")

    seed = _env_enablement()

    assert seed is not None
    assert seed["home_channel"] == {"chat_id": PHONE, "name": "Primary DM"}


def test_configured_project_key_needs_no_sign_in(photon_api, non_interactive, monkeypatch, capsys) -> None:
    """PHOTON_PROJECT_ID + PHOTON_API_KEY set by hand: no browser, no new key, routing verified."""
    monkeypatch.setenv("PHOTON_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_env")
    photon_api.on("GET", _PROJECT, photon_api.project())
    _line_routes(photon_api, texted=True)

    rc = cli._cmd_setup(_args())

    out = capsys.readouterr().out
    assert rc == 0 and "setup complete" in out and LINE in out
    assert "restart" not in out.lower()
    assert photon_api.calls("POST", f"{_PROJECT}/api-keys") == []
    assert {r.headers["Authorization"] for r in photon_api.requests} == {"Bearer pho_sk_env"}


def test_refused_key_is_replaced_and_the_old_one_revoked(photon_api, non_interactive, capsys) -> None:
    save_env_value("PHOTON_PROJECT_ID", PROJECT_ID)
    save_env_value("PHOTON_API_KEY", "pho_sk_old")
    photon_auth._store_pool_record("photon_project", {"project_id": PROJECT_ID, "api_key_id": "pho_key_old"})
    _signed_in()
    photon_api.on("GET", _PROJECT, photon_api.problem(401, "NOT_AUTHENTICATED"), photon_api.project())
    photon_api.on("POST", f"{_PROJECT}/api-keys", (201, {"apiKey": photon_api.api_key("pho_key_new"),
                                                          "secret": "pho_sk_new"}))
    photon_api.on("DELETE", f"{_PROJECT}/api-keys/pho_key_old", {"apiKey": photon_api.api_key("pho_key_old")})
    _line_routes(photon_api, texted=True)

    rc = cli._cmd_setup(_args())

    out = capsys.readouterr().out
    assert rc == 0 and "setup complete" in out and "hermes gateway restart" in out
    assert get_env_value("PHOTON_API_KEY") == "pho_sk_new"
    assert len(photon_api.calls("DELETE", f"{_PROJECT}/api-keys/pho_key_old")) == 1
    assert "pho_sk_new" not in out


def test_first_setup_creates_a_project_and_moves_spectrum_aside(photon_api, non_interactive, capsys) -> None:
    """A Spectrum-era home: a new Photon 2.0 project, key and line; the Spectrum credentials
    leave the runtime slots for auth.json; no first text yet, so routing is not reported live."""
    save_env_value("PHOTON_PROJECT_ID", "8f3c-spectrum")
    save_env_value("PHOTON_PROJECT_SECRET", "spectrum-secret")
    _signed_in()
    org_projects = f"/v1/organizations/{photon_api.ORG_ID}/projects"
    photon_api.on("GET", "/v1/organizations", {"acquisitionQuestionnaireExempt": False,
                                               "organizations": [photon_api.organization()],
                                               "ownedOrganizationIds": [photon_api.ORG_ID]})
    photon_api.on("GET", org_projects, {"projects": []})
    photon_api.on("POST", org_projects, (202, photon_api.project()))
    photon_api.on("POST", f"{_PROJECT}/api-keys", (201, {"apiKey": photon_api.api_key(), "secret": "pho_sk_new"}))
    _line_routes(photon_api, texted=False)
    photon_api.on("GET", _ASSIGNMENTS, {"assignments": []})
    photon_api.on("POST", _ASSIGNMENTS, photon_api.problem(403, "FORBIDDEN"), (201, photon_api.assignment(PHONE, LINE)))

    rc = cli._cmd_setup(_args())

    out = capsys.readouterr().out
    assert rc == 0
    assert "setup complete" not in out and "routing is NOT live" in out and LINE in out
    assert photon_api.body(photon_api.calls("POST", org_projects)[0])["name"] == "Hermes Agent"
    assert get_env_value("PHOTON_PROJECT_ID") == PROJECT_ID
    assert get_env_value("PHOTON_API_KEY") == "pho_sk_new"
    assert get_env_value("PHOTON_PROJECT_SECRET") is None
    legacy = photon_auth._pool_first(photon_auth._load_auth(), "photon_legacy")
    assert (legacy["spectrum_project_id"], legacy["project_secret"]) == ("8f3c-spectrum", "spectrum-secret")
    # The minted key cannot take a seat; the assignment falls back to the account.
    assert [r.headers["Authorization"] for r in photon_api.calls("POST", _ASSIGNMENTS)] == [
        "Bearer pho_sk_new", "Bearer acc-session"]


def test_setup_without_phone_does_not_report_complete(photon_api, non_interactive, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PHOTON_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_env")
    photon_api.on("GET", _PROJECT, photon_api.project())

    rc = cli._cmd_setup(_args(phone=None))

    out = capsys.readouterr().out
    assert rc == 0
    assert "setup complete" not in out
    assert "routing is NOT live" in out and "--phone" in out
    assert photon_api.calls("GET", _ASSIGNMENTS) == []
