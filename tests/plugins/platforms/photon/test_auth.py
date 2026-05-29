"""Tests for the Photon auth module (device login + project + user creation)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from plugins.platforms.photon import auth as photon_auth


# ---------------------------------------------------------------------------
# Fake httpx — we don't want to hit the real Photon API in unit tests.

class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        json_body: Any = None,
        headers: Dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def tmp_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("PHOTON_PROJECT_ID", raising=False)
    monkeypatch.delenv("PHOTON_PROJECT_SECRET", raising=False)
    monkeypatch.delenv("PHOTON_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PHOTON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PHOTON_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("PHOTON_ALLOW_ALL_USERS", raising=False)
    # The auth module memoises by reading get_hermes_home at call time
    # so the env var is what matters.
    return home


def test_store_and_load_photon_token(tmp_hermes_home: Path) -> None:
    photon_auth.store_photon_token("abc123def456")
    assert photon_auth.load_photon_token() == "abc123def456"

    env_text = (tmp_hermes_home / ".env").read_text(encoding="utf-8")
    assert "PHOTON_DASHBOARD_TOKEN=abc123def456" in env_text
    assert not (tmp_hermes_home / "auth.json").exists()


def test_clear_photon_token_removes_env_value(tmp_hermes_home: Path) -> None:
    photon_auth.store_photon_token("abc123def456")

    assert photon_auth.clear_photon_token() is True
    assert photon_auth.load_photon_token() is None
    assert "PHOTON_DASHBOARD_TOKEN" not in (
        tmp_hermes_home / ".env"
    ).read_text(encoding="utf-8")


def test_store_and_load_project_credentials(tmp_hermes_home: Path) -> None:
    photon_auth.store_project_credentials(
        "proj-uuid", "secret-key", name="Test Project",
    )
    pid, secret = photon_auth.load_project_credentials()
    assert pid == "proj-uuid"
    assert secret == "secret-key"

    env_text = (tmp_hermes_home / ".env").read_text(encoding="utf-8")
    assert "PHOTON_PROJECT_ID=proj-uuid" in env_text
    assert "PHOTON_PROJECT_SECRET=secret-key" in env_text
    assert not (tmp_hermes_home / "auth.json").exists()


def test_ensure_phone_allowed_adds_photon_allowlist(
    tmp_hermes_home: Path,
) -> None:
    result = photon_auth.ensure_phone_allowed("+15551234567")

    assert result == "added"
    assert photon_auth.load_allowed_phone_numbers() == ["+15551234567"]
    env_text = (tmp_hermes_home / ".env").read_text(encoding="utf-8")
    assert "PHOTON_ALLOWED_USERS=+15551234567" in env_text


def test_ensure_phone_allowed_preserves_existing_entries(
    tmp_hermes_home: Path,
) -> None:
    photon_auth.ensure_phone_allowed("+15551234567")
    result = photon_auth.ensure_phone_allowed("+15557654321")

    assert result == "added"
    assert photon_auth.load_allowed_phone_numbers() == [
        "+15551234567",
        "+15557654321",
    ]


def test_ensure_phone_allowed_dedupes_existing_phone(
    tmp_hermes_home: Path,
) -> None:
    photon_auth.ensure_phone_allowed("+15551234567")
    result = photon_auth.ensure_phone_allowed("+15551234567")

    assert result == "already_allowed"
    assert photon_auth.load_allowed_phone_numbers() == ["+15551234567"]


def test_ensure_phone_allowed_rejects_non_e164(
    tmp_hermes_home: Path,
) -> None:
    with pytest.raises(ValueError):
        photon_auth.ensure_phone_allowed("415-555-1234")


def test_ensure_phone_allowed_honors_allow_all(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOTON_ALLOW_ALL_USERS", "true")

    assert photon_auth.ensure_phone_allowed("+15551234567") == "allow_all"
    assert not (tmp_hermes_home / ".env").exists()


def test_load_project_credentials_reads_env_file_without_preload(
    tmp_hermes_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    photon_auth.store_project_credentials("from-file", "secret-file")
    monkeypatch.delenv("PHOTON_PROJECT_ID", raising=False)
    monkeypatch.delenv("PHOTON_PROJECT_SECRET", raising=False)
    pid, secret = photon_auth.load_project_credentials()
    assert pid == "from-file"
    assert secret == "secret-file"


def test_load_project_credentials_process_env_override(
    tmp_hermes_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    photon_auth.store_project_credentials("from-file", "secret-file")
    monkeypatch.setenv("PHOTON_PROJECT_ID", "from-env")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "secret-env")
    pid, secret = photon_auth.load_project_credentials()
    assert pid == "from-env"
    assert secret == "secret-env"


def test_request_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["body"] = json
        return _FakeResponse(json_body={
            "device_code": "dev-code-xyz",
            "user_code": "ABCD-1234",
            "verification_uri": "https://app.photon.codes/device",
            "verification_uri_complete": "https://app.photon.codes/device?code=ABCD-1234",
            "expires_in": 600,
            "interval": 5,
        })

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)

    code = photon_auth.request_device_code()
    assert code.device_code == "dev-code-xyz"
    assert code.user_code == "ABCD-1234"
    assert code.expires_in == 600
    assert "/api/auth/device/code" in captured["url"]
    assert captured["body"]["client_id"] == "photon-cli"
    assert captured["body"]["scope"] == "openid profile email"


def test_poll_for_token_rejects_header_only_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session header is not usable as the dashboard API token."""

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={"session": {}, "user": {}},
            headers={"set-auth-token": "bearer-xyz"},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)

    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    with pytest.raises(RuntimeError, match="no access_token"):
        photon_auth.poll_for_token(code, interval=0, timeout=2)


def test_poll_for_token_via_body_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body access token is the preferred API bearer token."""

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={"data": {"access_token": "from-body"}, "user": {}},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    assert photon_auth.poll_for_token(code, interval=0, timeout=2) == "from-body"


def test_poll_for_token_rejects_session_token_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better Auth session tokens pass get-session but fail project APIs."""

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={"session": {"token": "session-token"}, "user": {}},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    with pytest.raises(RuntimeError, match="no access_token"):
        photon_auth.poll_for_token(code, interval=0, timeout=2)


def test_poll_for_token_rejects_generic_token_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only access_token is accepted as the dashboard API bearer token."""

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(status=200, json_body={"token": "session-token"})

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    with pytest.raises(RuntimeError, match="no access_token"):
        photon_auth.poll_for_token(code, interval=0, timeout=2)


def test_poll_for_token_prefers_body_over_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore the short dashboard session header when body token exists."""

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={"access_token": "api-bearer-token"},
            headers={"set-auth-token": "short-session-token"},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    assert photon_auth.poll_for_token(code, interval=0, timeout=2) == "api-bearer-token"


def test_poll_for_token_debug_reports_sanitized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Dict[str, Any]] = []

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={
                "data": {"access_token": "secret-api-token"},
                "session": {"token": "secret-session-token"},
                "user": {"id": "user-id"},
            },
            headers={"set-auth-token": "secret-header-token"},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )

    token = photon_auth.poll_for_token(
        code,
        interval=0,
        timeout=2,
        on_debug=events.append,
    )

    assert token == "secret-api-token"
    assert events[0]["event"] == "device-token-response"
    assert events[0]["access_token_source"] == "data.access_token"
    assert events[0]["has_set_auth_token_header"] is True
    assert events[0]["token"]["length"] == len("secret-api-token")
    assert "access_token" in events[0]["data_keys"]
    assert "token" in events[0]["session_keys"]
    blob = json.dumps(events)
    assert "secret-api-token" not in blob
    assert "secret-session-token" not in blob
    assert "secret-header-token" not in blob


def test_validate_photon_token_accepts_session_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {"urls": []}

    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        captured["urls"].append(url)
        captured["headers"] = headers
        if "/api/projects/" in url:
            return _FakeResponse(json_body=[])
        return _FakeResponse(json_body={
            "session": {"id": "session-id"},
            "user": {"id": "user-id", "email": "user@example.com"},
        })

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    user = photon_auth.validate_photon_token("token-xyz")

    assert user["id"] == "user-id"
    assert captured["headers"]["Authorization"] == "Bearer token-xyz"
    assert any("/api/auth/get-session" in url for url in captured["urls"])
    assert any("/api/projects/" in url for url in captured["urls"])


def test_validate_photon_token_rejects_unrecognized_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        return _FakeResponse(json_body=None)

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    with pytest.raises(photon_auth.PhotonDashboardAuthError, match="did not recognize"):
        photon_auth.validate_photon_token("bad-token")


def test_validate_photon_token_rejects_project_api_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        if "/api/projects/" in url:
            return _FakeResponse(
                status=401,
                json_body={"error": "invalid_token"},
                text="invalid_token",
            )
        return _FakeResponse(json_body={
            "session": {"id": "session-id"},
            "user": {"id": "user-id", "email": "user@example.com"},
        })

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    with pytest.raises(photon_auth.PhotonDashboardAuthError, match="invalid_token"):
        photon_auth.validate_photon_token("session-token")


def test_dashboard_auth_diagnostics_are_sanitized(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photon_auth.store_photon_token("x" * 32)

    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        assert "x" * 32 in headers["Authorization"]
        if "/api/projects/" in url:
            return _FakeResponse(json_body=[{"id": "dash-1"}])
        if "/api/profile" in url:
            return _FakeResponse(status=401, text="invalid_token")
        return _FakeResponse(json_body={
            "session": {"token": "x" * 32},
            "user": {"id": "user-id"},
        })

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    diagnostics = photon_auth.dashboard_auth_diagnostics()
    blob = json.dumps(diagnostics)
    assert "x" * 32 not in blob
    assert diagnostics["token"]["length"] == 32
    assert diagnostics["token"]["dot_count"] == 0
    checks = {check["name"]: check for check in diagnostics["checks"]}
    assert checks["session"]["ok"] is True
    assert checks["profile"]["status"] == 401
    assert checks["projects"]["detail"] == "ok; projects=1"


def test_login_device_flow_validates_before_storing(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        photon_auth,
        "request_device_code",
        lambda client_id=photon_auth.DEFAULT_CLIENT_ID: photon_auth.DeviceCode(
            device_code="d",
            user_code="u",
            verification_uri="https://x",
            verification_uri_complete=None,
            expires_in=10,
            interval=0,
        ),
    )
    monkeypatch.setattr(
        photon_auth,
        "poll_for_token_candidates",
        lambda *_args, **_kwargs: [
            photon_auth._DeviceTokenCandidate(
                source="access_token",
                token="token",
            )
        ],
    )
    monkeypatch.setattr(
        photon_auth,
        "validate_photon_token",
        lambda _token: (_ for _ in ()).throw(
            photon_auth.PhotonDashboardAuthError("invalid")
        ),
    )

    with pytest.raises(photon_auth.PhotonDashboardAuthError):
        photon_auth.login_device_flow(open_browser=False)

    assert photon_auth.load_photon_token() is None


def test_login_device_flow_reports_no_project_valid_candidate(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        photon_auth,
        "request_device_code",
        lambda client_id=photon_auth.DEFAULT_CLIENT_ID: photon_auth.DeviceCode(
            device_code="d",
            user_code="u",
            verification_uri="https://x",
            verification_uri_complete=None,
            expires_in=10,
            interval=0,
        ),
    )
    monkeypatch.setattr(
        photon_auth,
        "poll_for_token_candidates",
        lambda *_args, **_kwargs: [
            photon_auth._DeviceTokenCandidate(
                source="access_token",
                token="session-only-token",
            )
        ],
    )
    monkeypatch.setattr(
        photon_auth,
        "validate_photon_token",
        lambda _token: (_ for _ in ()).throw(
            photon_auth.PhotonDashboardAuthError("invalid_token")
        ),
    )

    with pytest.raises(
        photon_auth.PhotonDashboardAuthError,
        match="no project-valid dashboard token candidate.*access_token",
    ) as exc_info:
        photon_auth.login_device_flow(open_browser=False)

    assert "session-only-token" not in str(exc_info.value)
    assert photon_auth.load_photon_token() is None


def test_login_device_flow_can_use_project_valid_header_candidate(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        photon_auth,
        "request_device_code",
        lambda client_id=photon_auth.DEFAULT_CLIENT_ID: photon_auth.DeviceCode(
            device_code="d",
            user_code="u",
            verification_uri="https://x",
            verification_uri_complete=None,
            expires_in=10,
            interval=0,
        ),
    )

    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            json_body={"access_token": "session-body-token"},
            headers={"set-auth-token": "project-header-token"},
        )

    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        token = headers["Authorization"].removeprefix("Bearer ")
        if token == "session-body-token" and "/api/projects/" in url:
            return _FakeResponse(
                status=401,
                json_body={"error": "invalid_token"},
                text="invalid_token",
            )
        return _FakeResponse(json_body={
            "session": {"id": "session-id"},
            "user": {"id": "user-id"},
        } if "/api/auth/get-session" in url else [])

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    token = photon_auth.login_device_flow(open_browser=False)

    assert token == "project-header-token"
    assert photon_auth.load_photon_token() == "project-header-token"


def test_poll_for_token_propagates_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, *, json: Dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=400, json_body={"error": "access_denied"},
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    code = photon_auth.DeviceCode(
        device_code="d", user_code="u",
        verification_uri="https://x", verification_uri_complete=None,
        expires_in=10, interval=0,
    )
    with pytest.raises(RuntimeError, match="access_denied"):
        photon_auth.poll_for_token(code, interval=0, timeout=2)


def test_create_user_rejects_invalid_phone() -> None:
    with pytest.raises(ValueError, match="E.164"):
        photon_auth.create_user(
            "proj", "secret", phone_number="not-a-number",
        )


def test_create_user_posts_shared_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_post(url: str, *, json: Dict[str, Any], auth: tuple, timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["body"] = json
        captured["auth"] = auth
        return _FakeResponse(json_body={
            "succeed": True,
            "data": {
                "id": "user-uuid",
                "phoneNumber": "+15551234567",
                "assignedPhoneNumber": "+15559999999",
            },
        })

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    user = photon_auth.create_user(
        "proj-id", "proj-secret",
        phone_number="+15551234567",
    )
    assert user["assignedPhoneNumber"] == "+15559999999"
    assert captured["auth"] == ("proj-id", "proj-secret")
    assert captured["body"]["type"] == "shared"
    assert captured["body"]["phoneNumber"] == "+15551234567"
    assert "/projects/proj-id/users/" in captured["url"]


def test_list_projects_normalizes_collection_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(json_body={
            "data": [
                {
                    "id": "dash-uuid",
                    "name": "Hermes Agent",
                    "spectrum": True,
                    "platforms": ["imessage"],
                    "spectrumProjectId": "spectrum-uuid",
                    "projectSecret": "secret",
                },
            ],
        })

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    projects = photon_auth.list_projects("token-xyz")
    normalized = photon_auth.normalize_project(projects[0])

    assert "/api/projects/" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token-xyz"
    assert normalized["dashboard_project_id"] == "dash-uuid"
    assert normalized["spectrum_project_id"] == "spectrum-uuid"
    assert normalized["project_secret"] == "secret"
    assert normalized["spectrum_enabled"] is True
    assert normalized["imessage_enabled"] is True


def test_normalize_project_accepts_single_data_wrapper() -> None:
    normalized = photon_auth.normalize_project({
        "data": {
            "id": "dash-uuid",
            "name": "Hermes Agent",
            "spectrum": True,
            "platforms": ["imessage"],
            "spectrumProjectId": "spectrum-uuid",
            "projectSecret": "secret",
        },
    })

    assert normalized["dashboard_project_id"] == "dash-uuid"
    assert normalized["spectrum_project_id"] == "spectrum-uuid"
    assert normalized["project_secret"] == "secret"


def test_list_projects_rejects_bad_dashboard_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            status=401,
            json_body={"error": "invalid_token"},
            text="invalid_token",
        )

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    with pytest.raises(photon_auth.PhotonDashboardAuthError, match="invalid_token"):
        photon_auth.list_projects("bad-token")


def test_create_project_rejects_bad_dashboard_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str,
        *,
        json: Dict[str, Any],
        headers: Dict[str, str],
        timeout: float,
    ) -> _FakeResponse:
        return _FakeResponse(
            status=401,
            json_body={"message": "Unauthorized"},
            text="Unauthorized",
        )

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)

    with pytest.raises(photon_auth.PhotonDashboardAuthError, match="Unauthorized"):
        photon_auth.create_project("bad-token", name="Hermes Agent")


def test_get_project_uses_dashboard_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    def fake_get(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(json_body={
            "id": "dash-1",
            "spectrumProjectId": "spectrum-1",
        })

    monkeypatch.setattr(photon_auth.httpx, "get", fake_get)

    project = photon_auth.get_project("token-xyz", "dash-1")

    assert "/api/projects/dash-1" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token-xyz"
    assert project["spectrumProjectId"] == "spectrum-1"


def test_regenerate_project_secret_uses_dashboard_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    def fake_post(url: str, *, headers: Dict[str, str], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(json_body={"projectSecret": "secret-1"})

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)

    data = photon_auth.regenerate_project_secret("token-xyz", "dash-1")

    assert "/api/projects/dash-1/regenerate-secret" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token-xyz"
    assert data["projectSecret"] == "secret-1"


def test_reusable_projects_match_requested_name_only() -> None:
    projects = [
        {
            "id": "unrelated",
            "name": "Customer Project",
            "spectrum": True,
            "platforms": ["imessage"],
            "spectrumProjectId": "other-spectrum",
            "projectSecret": "secret",
        },
        {
            "id": "hermes",
            "name": "Hermes Agent",
            "spectrum": True,
            "platforms": ["imessage"],
            "spectrumProjectId": "hermes-spectrum",
            "projectSecret": "secret",
        },
    ]

    reusable = photon_auth.reusable_projects(projects, preferred_name="Hermes Agent")

    assert len(reusable) == 1
    assert reusable[0]["dashboard_project_id"] == "hermes"


def test_register_webhook_surfaces_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, *, json: Dict[str, Any], auth: tuple, timeout: float) -> _FakeResponse:
        return _FakeResponse(json_body={
            "succeed": True,
            "data": {
                "id": "wh-uuid",
                "webhookUrl": json["webhookUrl"],
                "signingSecret": "0" * 64,
            },
        })

    monkeypatch.setattr(photon_auth.httpx, "post", fake_post)
    data = photon_auth.register_webhook(
        "proj", "secret", webhook_url="https://x.example.com/hook",
    )
    assert data["signingSecret"] == "0" * 64
    assert data["webhookUrl"] == "https://x.example.com/hook"


def test_persist_webhook_signing_secret_writes_env(
    tmp_hermes_home: Path,
) -> None:
    """The helper hands the secret to save_env_value, never returns it."""
    summary: list = []
    response = {
        "id": "wh-uuid",
        "webhookUrl": "https://x.example.com/hook",
        "signingSecret": "ABCDEF1234567890" * 4,
    }
    ok = photon_auth.persist_webhook_signing_secret(
        response, on_summary=summary.append,
    )

    assert ok is True
    env_path = tmp_hermes_home / ".env"
    assert env_path.exists()
    env_text = env_path.read_text()
    assert "PHOTON_WEBHOOK_SECRET=ABCDEF1234567890" in env_text
    # The on_summary callback gets the redacted response + a saved-to path;
    # none of those strings should leak the raw secret.
    joined = "\n".join(summary)
    assert "<redacted>" in joined
    assert "ABCDEF1234567890" not in joined


def test_persist_webhook_signing_secret_no_secret_no_write(
    tmp_hermes_home: Path,
) -> None:
    summary: list = []
    ok = photon_auth.persist_webhook_signing_secret(
        {"id": "wh-uuid", "webhookUrl": "https://x"},
        on_summary=summary.append,
    )
    assert ok is False
    # No env file written; summary callback still received the redacted
    # response (without a signingSecret key, nothing to redact).
    assert not (tmp_hermes_home / ".env").exists()


def test_credential_summary_returns_only_display_strings(
    tmp_hermes_home: Path,
) -> None:
    """credential_summary must not leak raw token/secret material."""
    photon_auth.store_photon_token("token-aaaaaaaaaaaaaaaa")
    photon_auth.store_project_credentials("proj-uuid", "secret-bbbbbbbbbbb")
    summary = photon_auth.credential_summary()
    blob = "\n".join(summary.values())
    assert "token-aaaa" not in blob
    assert "secret-bbbb" not in blob
    assert summary["device_token"].startswith("✓")
    assert summary["project_key"].startswith("✓")
    assert summary["project_id"] == "proj-uuid"


def test_print_credential_summary_emits_only_display_strings(
    tmp_hermes_home: Path,
) -> None:
    """The emit callback must never receive raw credential bytes."""
    photon_auth.store_photon_token("token-aaaaaaaaaaaaaaaa")
    photon_auth.store_project_credentials("proj-uuid", "secret-bbbbbbbbbbb")
    lines: list = []
    photon_auth.print_credential_summary(lines.append)
    blob = "\n".join(lines)
    assert "token-aaaa" not in blob
    assert "secret-bbbb" not in blob
    assert "✓ stored" in blob   # device token line
    assert "proj-uuid" in blob   # project id is intentionally surfaced
    # Header is always emitted
    assert any("Photon iMessage status" in line for line in lines)
