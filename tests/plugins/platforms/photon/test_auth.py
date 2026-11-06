"""Photon 2.0 setup client (auth.py) against an in-process Photon API (see conftest.py)."""
from __future__ import annotations

import functools
import json

import pytest

from hermes_cli.config import get_env_value, save_env_value
from plugins.platforms.photon import auth as photon_auth

PROJECT_ID = "pho_prj_00000000000000000000000001"
PHONE = "+15551234567"
_ASSIGNMENTS = f"/v1/projects/{PROJECT_ID}/platforms/imessage/assignments"
_TOKEN = "/v1/auth/device/token"


def _auth_json() -> dict:
    return json.loads(photon_auth._auth_json_path().read_text(encoding="utf-8"))


def _pool(key: str) -> list:
    return _auth_json().get("credential_pool", {}).get(key)


def _device_code_reply() -> dict:
    return {"device_code": "dev-code", "expires_in": 300, "interval": 5, "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.photon.codes/device",
            "verification_uri_complete": "https://auth.photon.codes/device?user_code=ABCD-EFGH"}


# -- Device login -----------------------------------------------------------------

def test_device_login_waits_for_approval_and_stores_the_session(photon_api, monkeypatch) -> None:
    photon_api.on("POST", "/v1/auth/device/code", _device_code_reply())
    photon_api.on("POST", _TOKEN, (400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}),
                  photon_api.token_grant(access="acc-1", refresh="ref-1"))
    sleeps: list[float] = []
    monkeypatch.setattr(photon_auth, "poll_for_token",
                        functools.partial(photon_auth.poll_for_token, sleep_fn=sleeps.append))
    shown: list = []

    token = photon_auth.login_device_flow(open_browser=False, on_user_code=shown.append)

    assert token == "acc-1"
    assert shown[0].user_code == "ABCD-EFGH"
    assert sleeps == [5, 5, 10]  # pending keeps the interval, slow_down adds 5 s (RFC 8628)
    assert {photon_api.body(r)["grant_type"] for r in photon_api.calls("POST", _TOKEN)} == {
        photon_auth.DEVICE_CODE_GRANT}
    assert _pool("photon")[0]["refresh_token"] == "ref-1"
    assert photon_auth.signed_in_email() == "ops@example.com"


def test_denied_device_login_aborts(photon_api) -> None:
    photon_api.on("POST", _TOKEN, (400, {"error": "access_denied"}))
    code = photon_auth.DeviceCode("dev-code", "ABCD-EFGH", "https://auth.photon.codes/device", None, 300, 5)

    with pytest.raises(RuntimeError, match="access_denied"):
        photon_auth.poll_for_token(code, sleep_fn=lambda _s: None)
    assert len(photon_api.calls("POST", _TOKEN)) == 1


def test_expired_session_refreshes_and_keeps_the_rotated_refresh_token(photon_api) -> None:
    photon_auth._store_pool_record("photon", {"access_token": "acc-0", "refresh_token": "ref-0", "expires_at": 0})
    photon_api.on("POST", _TOKEN, photon_api.token_grant(access="acc-2", refresh="ref-2"))

    assert photon_auth.load_account_token() == "acc-2"
    assert photon_api.body(photon_api.calls("POST", _TOKEN)[0]) == {
        "grant_type": "refresh_token", "refresh_token": "ref-0"}
    assert _pool("photon")[0]["refresh_token"] == "ref-2"


def test_refused_refresh_forgets_the_session(photon_api) -> None:
    photon_auth._store_pool_record("photon", {"access_token": "acc-0", "refresh_token": "ref-0", "expires_at": 0})
    photon_api.on("POST", _TOKEN, (400, {"error": "invalid_grant"}))

    assert photon_auth.load_account_token() is None
    assert _pool("photon") == []


# -- Runtime credentials ------------------------------------------------------------

def test_project_key_goes_to_env_and_only_ids_to_auth_json(photon_home) -> None:
    photon_auth.store_project_credentials(
        project_id=PROJECT_ID, api_key="pho_sk_live", organization_id="pho_org_1", api_key_id="pho_key_1")

    assert get_env_value("PHOTON_PROJECT_ID") == PROJECT_ID
    assert get_env_value("PHOTON_API_KEY") == "pho_sk_live"
    assert photon_auth.load_runtime_credentials() == (PROJECT_ID, "pho_sk_live")
    assert _pool("photon_project")[0]["api_key_id"] == "pho_key_1"
    assert "pho_sk_live" not in photon_auth._auth_json_path().read_text(encoding="utf-8")


def test_spectrum_project_id_is_not_a_photon_2_project(photon_home, monkeypatch) -> None:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "8f3c-spectrum")
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_live")

    assert photon_auth.load_runtime_credentials() == (None, "pho_sk_live")


def test_spectrum_credentials_move_out_of_the_runtime_slots(photon_home) -> None:
    save_env_value("PHOTON_PROJECT_ID", "8f3c-spectrum")
    save_env_value("PHOTON_PROJECT_SECRET", "spectrum-secret")
    photon_auth._store_pool_record("photon_project", {
        "spectrum_project_id": "8f3c-spectrum", "project_secret": "spectrum-secret"})
    assert photon_auth.has_legacy_credentials()

    assert photon_auth.archive_legacy_credentials() is True

    legacy = _pool("photon_legacy")[0]
    assert (legacy["spectrum_project_id"], legacy["project_secret"]) == ("8f3c-spectrum", "spectrum-secret")
    assert get_env_value("PHOTON_PROJECT_ID") is None and get_env_value("PHOTON_PROJECT_SECRET") is None
    assert _pool("photon_project") == []
    assert not photon_auth.has_legacy_credentials()
    assert photon_auth.archive_legacy_credentials() is False


def test_photon_2_project_id_in_env_keeps_the_spectrum_pairing(photon_home, monkeypatch) -> None:
    """Photon 2.0 credentials set by hand on a Spectrum home: the Spectrum id still comes from
    auth.json, and the hand-set Photon 2.0 id stays in place."""
    monkeypatch.setenv("PHOTON_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_live")
    photon_auth._store_pool_record("photon_project", {
        "spectrum_project_id": "8f3c-spectrum", "project_secret": "spectrum-secret"})

    assert photon_auth.archive_legacy_credentials() is True

    legacy = _pool("photon_legacy")[0]
    assert (legacy["spectrum_project_id"], legacy["project_secret"]) == ("8f3c-spectrum", "spectrum-secret")
    assert photon_auth.load_runtime_credentials() == (PROJECT_ID, "pho_sk_live")


# -- Projects and keys ----------------------------------------------------------------

def test_find_project_by_name_follows_pages(photon_api) -> None:
    path = f"/v1/organizations/{photon_api.ORG_ID}/projects"
    photon_api.on("GET", path,
                  {"projects": [photon_api.project("pho_prj_other", "Other")], "nextPageToken": "p2"},
                  {"projects": [photon_api.project(PROJECT_ID, "hermes agent")]})

    assert photon_auth.find_project_by_name("acc", photon_api.ORG_ID, "Hermes Agent") == PROJECT_ID
    assert photon_api.calls("GET", path)[1].url.params["pageToken"] == "p2"


def test_key_limit_names_the_fix(photon_api) -> None:
    photon_api.on("POST", f"/v1/projects/{PROJECT_ID}/api-keys", photon_api.problem(409, "API_KEY_LIMIT_REACHED"))

    with pytest.raises(photon_auth.PhotonAuthError, match="revoke an unused key"):
        photon_auth.mint_project_key("acc", PROJECT_ID)


def test_minted_key_is_returned_once_with_its_id(photon_api) -> None:
    path = f"/v1/projects/{PROJECT_ID}/api-keys"
    photon_api.on("POST", path, (201, {"apiKey": photon_api.api_key("pho_key_9"), "secret": "pho_sk_new"}))

    assert photon_auth.mint_project_key("acc", PROJECT_ID) == ("pho_key_9", "pho_sk_new")
    request = photon_api.calls("POST", path)[0]
    assert request.headers["Authorization"] == "Bearer acc" and request.headers["Idempotency-Key"]


def test_rejected_project_key_is_reported(photon_api) -> None:
    photon_api.on("GET", f"/v1/projects/{PROJECT_ID}", photon_api.problem(401, "NOT_AUTHENTICATED"))

    assert photon_auth.project_key_rejected(PROJECT_ID, "pho_sk_old") is True


# -- Shared iMessage line ---------------------------------------------------------------

def test_existing_assignment_is_reused(photon_api) -> None:
    photon_api.on("GET", _ASSIGNMENTS, {"assignments": [photon_api.assignment(PHONE)]})

    found, created = photon_auth.ensure_line_assignment("pho_sk", PROJECT_ID, phone=PHONE)

    assert (found["assigned_phone_number"], created) == ("+16282679185", False)
    assert photon_api.calls("POST", _ASSIGNMENTS) == []


def test_new_assignment_is_created_once(photon_api) -> None:
    photon_api.on("GET", _ASSIGNMENTS, {"assignments": []})
    photon_api.on("POST", _ASSIGNMENTS, (201, photon_api.assignment(PHONE)))

    found, created = photon_auth.ensure_line_assignment("acc", PROJECT_ID, phone=PHONE, email="ops@example.com")

    assert created and found["handle"] == PHONE
    request = photon_api.calls("POST", _ASSIGNMENTS)[0]
    assert photon_api.body(request) == {"handle": PHONE, "email": "ops@example.com"}
    assert request.headers["Idempotency-Key"]


def test_concurrent_assignment_resolves_to_the_existing_one(photon_api) -> None:
    photon_api.on("GET", _ASSIGNMENTS, {"assignments": []}, {"assignments": [photon_api.assignment(PHONE)]})
    photon_api.on("POST", _ASSIGNMENTS, photon_api.problem(409, "ASSIGNMENT_EXISTS"))

    found, created = photon_auth.ensure_line_assignment("acc", PROJECT_ID, phone=PHONE)

    assert (found["handle"], created) == (PHONE, False)


def test_assignment_needs_an_e164_number(photon_api) -> None:
    with pytest.raises(ValueError, match="E.164"):
        photon_auth.ensure_line_assignment("acc", PROJECT_ID, phone="555-123-4567")
    assert photon_api.requests == []


def test_first_text_is_detected_from_received_messages(photon_api) -> None:
    path = f"/v1/projects/{PROJECT_ID}/messages"
    photon_api.on("GET", path, {"items": [photon_api.received_text("+15550000000")], "nextPageToken": "p2"},
                  {"items": [photon_api.received_text(PHONE)]})

    assert photon_auth.has_texted_line("pho_sk", PROJECT_ID, PHONE, photon_api.NOW) is True
    first = photon_api.calls("GET", path)[0].url.params
    assert (first["createdSince"], first["direction"]) == (photon_api.NOW, "received")


def test_no_first_text_yet(photon_api) -> None:
    photon_api.on("GET", f"/v1/projects/{PROJECT_ID}/messages", {"items": [photon_api.received_text("+15550000000")]})

    assert photon_auth.has_texted_line("pho_sk", PROJECT_ID, PHONE, photon_api.NOW) is False
