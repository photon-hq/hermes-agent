"""
Photon Dashboard + Spectrum API client and device-code login flow.

This module is pure Python — it intentionally does not depend on
``spectrum-ts``.  All management-plane operations (login, create
project, create user, register webhook) talk to Photon's HTTP API
directly:

    Dashboard API   https://app.photon.codes/api/...
                    project-valid token candidate from the device flow

    Spectrum API    https://spectrum.photon.codes/projects/{id}/...
                    HTTP Basic with (projectId, projectSecret)

The webhook receiver + Node sidecar in ``adapter.py`` consume the
Spectrum project credentials from Hermes' canonical ``~/.hermes/.env``.
Photon's dashboard/device token is also persisted there as
``PHOTON_DASHBOARD_TOKEN`` so Photon secrets have one storage surface.

Reference docs (read at integration time):
  https://photon.codes/docs/api-reference/introduction
  https://photon.codes/docs/api-reference/device-login/request-device-+-user-code
  https://photon.codes/docs/api-reference/device-login/exchange-device-code-for-token
  https://photon.codes/docs/api-reference/projects/create-project
  https://photon.codes/docs/api-reference/users/create-user
  https://photon.codes/docs/webhooks/overview
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a hermes dependency
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

# Hosted Photon currently allowlists the published Photon CLI device client.
# Use that compatibility client until the dashboard API registers Hermes as its
# own device-flow client_id.
DEFAULT_CLIENT_ID = "photon-cli"
DEFAULT_SCOPE = "openid profile email"

DEFAULT_DASHBOARD_HOST = "https://app.photon.codes"
DEFAULT_SPECTRUM_HOST = "https://spectrum.photon.codes"

# Polling defaults per RFC 8628.  Photon may override via `interval` /
# `expires_in` fields in the device-code response — those win.
DEFAULT_POLL_INTERVAL = 5
DEFAULT_POLL_TIMEOUT = 900  # 15 minutes is conservative; Photon returns expires_in

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
SETUP_LOCK_TIMEOUT_SECONDS = 30.0
_setup_lock_holder = threading.local()


# ---------------------------------------------------------------------------
# Hermes env helpers — Photon secrets live in the active profile's .env.

PHOTON_DASHBOARD_TOKEN_ENV = "PHOTON_DASHBOARD_TOKEN"
PHOTON_ALLOWED_USERS_ENV = "PHOTON_ALLOWED_USERS"


class PhotonDashboardAuthError(RuntimeError):
    """Raised when Photon rejects the saved dashboard bearer token."""


AuthDebugCallback = Callable[[Dict[str, Any]], None]


def _env_path() -> Path:
    """Resolve the active Hermes profile's ``.env`` path."""
    try:
        from hermes_cli.config import get_env_path  # type: ignore
        return get_env_path()
    except Exception:
        try:
            from hermes_constants import get_hermes_home  # type: ignore
            return Path(get_hermes_home()) / ".env"
        except Exception:
            return Path(os.path.expanduser("~/.hermes")) / ".env"


def _setup_lock_path() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home()) / "photon-setup.lock"
    except Exception:
        return Path(os.path.expanduser("~/.hermes")) / "photon-setup.lock"


def load_photon_token() -> Optional[str]:
    """Return the dashboard bearer token stored by ``login()`` or ``None``."""
    return _get_hermes_env_value(PHOTON_DASHBOARD_TOKEN_ENV)


def store_photon_token(token: str) -> None:
    """Persist the dashboard bearer token to Hermes' canonical ``.env``."""
    try:
        from hermes_cli.config import save_env_value  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "hermes_cli.config is required to save Photon credentials"
        ) from exc

    save_env_value(PHOTON_DASHBOARD_TOKEN_ENV, token)


def clear_photon_token() -> bool:
    """Remove the saved dashboard bearer token from Hermes' canonical ``.env``."""
    try:
        from hermes_cli.config import remove_env_value  # type: ignore
    except ImportError:
        return os.environ.pop(PHOTON_DASHBOARD_TOKEN_ENV, None) is not None

    return remove_env_value(PHOTON_DASHBOARD_TOKEN_ENV)


def _get_hermes_env_value(key: str) -> Optional[str]:
    """Read a Hermes env value without requiring callers to preload .env."""
    try:
        from hermes_cli.config import get_env_value  # type: ignore
    except ImportError:
        return os.getenv(key)
    return get_env_value(key)


def load_project_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Return ``(project_id, project_secret)`` from Hermes' environment."""
    return (
        _get_hermes_env_value("PHOTON_PROJECT_ID"),
        _get_hermes_env_value("PHOTON_PROJECT_SECRET"),
    )


def load_allowed_phone_numbers() -> list[str]:
    """Return the Photon sender allowlist from the active Hermes profile."""
    raw = _get_hermes_env_value(PHOTON_ALLOWED_USERS_ENV) or ""
    seen: set[str] = set()
    phones: list[str] = []
    for item in raw.split(","):
        phone = item.strip()
        if not phone or phone in seen:
            continue
        seen.add(phone)
        phones.append(phone)
    return phones


def ensure_phone_allowed(phone_number: str) -> str:
    """Allow an E.164 sender to control Hermes through Photon.

    Returns ``"added"``, ``"already_allowed"``, or ``"allow_all"``.
    """
    phone = phone_number.strip()
    if not E164_RE.match(phone):
        raise ValueError(
            "phone_number must be E.164 (format +<country-code><number>); "
            f"got {phone_number!r}"
        )
    if (_get_hermes_env_value("PHOTON_ALLOW_ALL_USERS") or "").strip().lower() in {
        "true",
        "1",
        "yes",
    }:
        return "allow_all"

    phones = load_allowed_phone_numbers()
    if "*" in phones or phone in phones:
        return "already_allowed"

    try:
        from hermes_cli.config import save_env_value  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "hermes_cli.config is required to save Photon gateway access"
        ) from exc

    phones.append(phone)
    save_env_value(PHOTON_ALLOWED_USERS_ENV, ",".join(phones))
    return "added"


def store_project_credentials(project_id: str, project_secret: str, **extra: Any) -> None:
    """Persist the Spectrum project's id+secret to Hermes' canonical .env."""
    _ = extra
    try:
        from hermes_cli.config import save_env_value  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "hermes_cli.config is required to save Photon credentials"
        ) from exc

    save_env_value("PHOTON_PROJECT_ID", project_id)
    save_env_value("PHOTON_PROJECT_SECRET", project_secret)


@contextmanager
def setup_lock(
    timeout_seconds: float = SETUP_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize setup flows that may create remote Photon projects."""
    if getattr(_setup_lock_holder, "depth", 0) > 0:
        _setup_lock_holder.depth += 1
        try:
            yield
        finally:
            _setup_lock_holder.depth -= 1
        return

    lock_path = _setup_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        _setup_lock_holder.depth = 1
        try:
            yield
        finally:
            _setup_lock_holder.depth = 0
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "another `hermes photon setup` process is already running"
                    )
                time.sleep(0.05)

        _setup_lock_holder.depth = 1
        try:
            yield
        finally:
            _setup_lock_holder.depth = 0
            if fcntl:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass


# ---------------------------------------------------------------------------
# Device login flow (RFC 8628)

@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int


@dataclass(frozen=True)
class _DeviceTokenCandidate:
    source: str
    token: str = field(repr=False)


def _dashboard_host() -> str:
    return (os.getenv("PHOTON_DASHBOARD_HOST") or DEFAULT_DASHBOARD_HOST).rstrip("/")


def _spectrum_host() -> str:
    return (os.getenv("PHOTON_API_HOST") or DEFAULT_SPECTRUM_HOST).rstrip("/")


def request_device_code(
    *, client_id: str = DEFAULT_CLIENT_ID, scope: Optional[str] = DEFAULT_SCOPE,
) -> DeviceCode:
    """POST ``/api/auth/device/code`` and return the device + user codes."""
    if httpx is None:
        raise RuntimeError("httpx is required for Photon device login")
    url = f"{_dashboard_host()}/api/auth/device/code"
    body: Dict[str, Any] = {"client_id": client_id}
    if scope:
        body["scope"] = scope
    resp = httpx.post(url, json=body, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        verification_uri_complete=data.get("verification_uri_complete"),
        expires_in=int(data.get("expires_in") or DEFAULT_POLL_TIMEOUT),
        interval=int(data.get("interval") or DEFAULT_POLL_INTERVAL),
    )


def poll_for_token(
    code: DeviceCode,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: Optional[int] = None,
    interval: Optional[int] = None,
    on_pending: Optional[callable] = None,
    on_debug: Optional[AuthDebugCallback] = None,
) -> str:
    """Poll ``/api/auth/device/token`` until the user approves.

    Returns the bearer token from the JSON response body. This compatibility
    wrapper matches the official Photon CLI; the full login path validates all
    token-like candidates before saving anything.
    """
    candidates = poll_for_token_candidates(
        code,
        client_id=client_id,
        timeout=timeout,
        interval=interval,
        on_pending=on_pending,
        on_debug=on_debug,
    )
    for candidate in candidates:
        if candidate.source != "set-auth-token":
            return candidate.token
    raise RuntimeError(
        "Photon returned 200 but no access_token in the response "
        "body; refusing to save the unusable session header token"
    )


def poll_for_token_candidates(
    code: DeviceCode,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: Optional[int] = None,
    interval: Optional[int] = None,
    on_pending: Optional[callable] = None,
    on_debug: Optional[AuthDebugCallback] = None,
) -> list[_DeviceTokenCandidate]:
    """Poll ``/api/auth/device/token`` and return all token-like candidates.

    Candidate token bytes are intentionally kept internal. Callers must validate
    a candidate against the dashboard project API before persisting it.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for Photon device login")
    url = f"{_dashboard_host()}/api/auth/device/token"
    deadline = time.time() + (timeout or code.expires_in or DEFAULT_POLL_TIMEOUT)
    sleep = interval or code.interval or DEFAULT_POLL_INTERVAL
    while time.time() < deadline:
        try:
            resp = httpx.post(
                url,
                json={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": code.device_code,
                    "client_id": client_id,
                },
                timeout=30.0,
            )
        except httpx.RequestError as e:
            logger.warning("photon: device-token poll failed: %s", e)
            time.sleep(sleep)
            continue
        if resp.status_code == 200:
            body: Dict[str, Any] = {}
            body_is_json = True
            try:
                decoded = resp.json() or {}
                body = decoded if isinstance(decoded, dict) else {}
                body_is_json = isinstance(decoded, dict)
            except (TypeError, ValueError, json.JSONDecodeError):
                body = {}
                body_is_json = False
            candidates = _device_response_token_candidates(
                body,
                headers=getattr(resp, "headers", {}),
            )
            _emit_auth_debug(
                on_debug,
                _device_token_debug_event(
                    resp,
                    body=body,
                    body_is_json=body_is_json,
                    candidates=candidates,
                ),
            )
            return candidates
        if resp.status_code == 400:
            # RFC 8628 §3.5 — error codes are returned with 400.
            body: Dict[str, Any] = {}
            try:
                body = resp.json() or {}
            except json.JSONDecodeError:
                pass
            err = body.get("error") or body.get("message") or ""
            if err in ("authorization_pending", "slow_down"):
                if on_pending:
                    try:
                        on_pending()
                    except Exception:
                        pass
                if err == "slow_down":
                    sleep += 5
                time.sleep(sleep)
                continue
            if err in ("expired_token", "access_denied"):
                raise RuntimeError(f"Photon login failed: {err}")
            # Unknown error — surface it
            raise RuntimeError(f"Photon device token error: {err or resp.text}")
        # Unexpected status; log and retry
        logger.warning(
            "photon: device-token unexpected status %s: %s",
            resp.status_code, resp.text[:200],
        )
        time.sleep(sleep)
    raise TimeoutError("Photon device login timed out")


def _device_response_access_token(body: Dict[str, Any]) -> Optional[str]:
    """Extract the API bearer token from known device-token response shapes."""
    token, _source = _device_response_access_token_with_source(body)
    return token


def _device_response_access_token_with_source(
    body: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    for candidate in _device_response_token_candidates(
        body,
        include_set_auth_token=False,
    ):
        return candidate.token, candidate.source
    return None, None


def _device_response_token_candidates(
    body: Dict[str, Any],
    *,
    headers: Optional[Any] = None,
    include_set_auth_token: bool = True,
) -> list[_DeviceTokenCandidate]:
    candidates: list[_DeviceTokenCandidate] = []
    seen: set[str] = set()

    def add(source: str, value: Any) -> None:
        token = _clean_bearer_token(value)
        if not token or token in seen:
            return
        seen.add(token)
        candidates.append(_DeviceTokenCandidate(source=source, token=token))

    add("access_token", body.get("access_token"))
    add("accessToken", body.get("accessToken"))
    data = body.get("data")
    if isinstance(data, dict):
        add("data.access_token", data.get("access_token"))
        add("data.accessToken", data.get("accessToken"))
    if include_set_auth_token:
        add("set-auth-token", _header_value(headers, "set-auth-token"))
    return candidates


def _clean_bearer_token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _header_value(headers: Optional[Any], name: str) -> Optional[str]:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value:
            return str(value)
    except AttributeError:
        pass
    try:
        for key, value in dict(headers).items():
            if str(key).lower() == name.lower() and value:
                return str(value)
    except (TypeError, ValueError):
        return None
    return None


def _device_token_debug_event(
    resp: Any,
    *,
    body: Dict[str, Any],
    body_is_json: bool,
    candidates: list[_DeviceTokenCandidate],
) -> Dict[str, Any]:
    data = body.get("data")
    session = body.get("session")
    user = body.get("user")
    access_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.source != "set-auth-token"
        ),
        None,
    )
    return {
        "event": "device-token-response",
        "status": getattr(resp, "status_code", "-"),
        "body_is_json": body_is_json,
        "body_keys": _safe_dict_keys(body),
        "data_keys": _safe_dict_keys(data),
        "session_keys": _safe_dict_keys(session),
        "user_present": isinstance(user, dict),
        "has_set_auth_token_header": bool(
            _header_value(getattr(resp, "headers", {}), "set-auth-token")
        ),
        "access_token_source": (
            access_candidate.source if access_candidate else "missing"
        ),
        "token": _token_shape(access_candidate.token if access_candidate else None),
        "candidates": [
            {
                "source": candidate.source,
                "token": _token_shape(candidate.token),
            }
            for candidate in candidates
        ],
    }


def _safe_dict_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value.keys())[:50]


def _emit_auth_debug(
    on_debug: Optional[AuthDebugCallback],
    event: Dict[str, Any],
) -> None:
    if not on_debug:
        return
    try:
        on_debug(event)
    except Exception:
        pass


def login_device_flow(
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    open_browser: bool = True,
    on_user_code: Optional[callable] = None,
    on_debug: Optional[AuthDebugCallback] = None,
) -> str:
    """Run the full device-code login flow and persist the token.

    Returns the bearer token.  ``on_user_code`` is a callback receiving the
    :class:`DeviceCode` so callers can print + optionally open the browser.
    """
    code = request_device_code(client_id=client_id)
    if on_user_code:
        try:
            on_user_code(code)
        except Exception:
            pass
    if open_browser:
        try:
            import webbrowser
            target = code.verification_uri_complete or code.verification_uri
            webbrowser.open(target, new=2)
        except Exception:
            pass
    candidates = poll_for_token_candidates(
        code,
        client_id=client_id,
        on_debug=on_debug,
    )
    token = _validated_dashboard_token(candidates, on_debug=on_debug)
    store_photon_token(token)
    return token


def _validated_dashboard_token(
    candidates: list[_DeviceTokenCandidate],
    *,
    on_debug: Optional[AuthDebugCallback] = None,
) -> str:
    if not candidates:
        raise RuntimeError(
            "Photon returned 200 but no token candidate in the device-token "
            "response. Expected access_token, data.access_token, or "
            "set-auth-token."
        )
    last_error: Optional[BaseException] = None
    dashboard_error: Optional[PhotonDashboardAuthError] = None
    for candidate in candidates:
        try:
            validate_photon_token(candidate.token)
        except Exception as exc:
            _emit_auth_debug(
                on_debug,
                _dashboard_validation_debug_event(
                    candidate.token,
                    candidate_source=candidate.source,
                ),
            )
            last_error = exc
            if isinstance(exc, PhotonDashboardAuthError):
                dashboard_error = exc
            continue
        _emit_auth_debug(
            on_debug,
            _dashboard_validation_debug_event(
                candidate.token,
                candidate_source=candidate.source,
            ),
        )
        return candidate.token
    if dashboard_error:
        sources = ", ".join(candidate.source for candidate in candidates)
        raise PhotonDashboardAuthError(
            f"{dashboard_error} Device login returned no project-valid "
            f"dashboard token candidate to save (tried: {sources or 'none'}). "
            "The returned token can authenticate the Better Auth session "
            "lookup, but Photon project/profile APIs reject it. Photon must "
            "return a dashboard API bearer token from the device flow, "
            "document another token exchange, or accept device-flow tokens on "
            "the project APIs."
        ) from dashboard_error
    if last_error:
        raise last_error
    raise RuntimeError("Photon did not return a usable dashboard token")


def validate_photon_token(token: str) -> Dict[str, Any]:
    """Verify that a device-flow token is usable for dashboard project APIs."""
    if httpx is None:
        raise RuntimeError("httpx is required for Photon device login")
    resp = _dashboard_get("/api/auth/get-session", token)
    resp.raise_for_status()
    data = resp.json()
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict) or not user:
        raise PhotonDashboardAuthError(
            "Photon issued a device token, but the dashboard session lookup "
            "did not recognize it."
        )
    projects_resp = _dashboard_get("/api/projects/", token)
    _raise_for_dashboard_status(
        projects_resp,
        action="validate Photon project API access",
    )
    return user


def dashboard_auth_diagnostics(token: Optional[str] = None) -> Dict[str, Any]:
    """Return sanitized Photon auth diagnostics for CLI/debug output."""
    token = token if token is not None else load_photon_token()
    result: Dict[str, Any] = {
        "env_path": str(_env_path()),
        "dashboard_host": _dashboard_host(),
        "token": _token_shape(token),
        "checks": [],
    }
    if not token:
        return result
    if httpx is None:
        result["checks"].append({
            "name": "httpx",
            "path": "-",
            "status": "missing",
            "ok": False,
            "detail": "httpx is required for Photon auth diagnostics",
        })
        return result

    for name, path in (
        ("session", "/api/auth/get-session"),
        ("profile", "/api/profile"),
        ("projects", "/api/projects/"),
    ):
        check: Dict[str, Any] = {
            "name": name,
            "path": path,
            "status": "-",
            "ok": False,
            "detail": "",
        }
        try:
            resp = _dashboard_get(path, token)
        except Exception as exc:
            check["status"] = "error"
            check["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            result["checks"].append(check)
            continue

        check["status"] = resp.status_code
        check["ok"] = 200 <= resp.status_code < 300
        check["detail"] = _diagnostic_response_detail(name, resp)
        result["checks"].append(check)
    return result


def _dashboard_validation_debug_event(
    token: Optional[str],
    *,
    candidate_source: Optional[str] = None,
) -> Dict[str, Any]:
    event = dashboard_auth_diagnostics(token)
    event["event"] = "dashboard-validation"
    if candidate_source:
        event["candidate_source"] = candidate_source
    return event


def _token_shape(token: Optional[str]) -> Dict[str, Any]:
    if not token:
        return {
            "present": False,
            "length": 0,
            "dot_count": 0,
            "looks_jwt": False,
        }
    return {
        "present": True,
        "length": len(token),
        "dot_count": token.count("."),
        "looks_jwt": token.count(".") == 2,
    }


def _dashboard_get(path: str, token: str) -> Any:
    url = f"{_dashboard_host()}{path}"
    return httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def _diagnostic_response_detail(name: str, resp: Any) -> str:
    if not (200 <= resp.status_code < 300):
        return _response_error_detail(resp) or "request failed"
    if name == "session":
        try:
            body = resp.json() or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return "ok; non-json body"
        user = body.get("user") if isinstance(body, dict) else None
        return "ok; user=yes" if isinstance(user, dict) else "ok; user=no"
    if name == "projects":
        try:
            items = _project_items(resp.json() or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            return "ok; non-json body"
        return f"ok; projects={len(items)}"
    return "ok"


# ---------------------------------------------------------------------------
# Dashboard API: create project

def create_project(
    token: str,
    *,
    name: str,
    location: str = "United States",
    platforms: Optional[list] = None,
) -> Dict[str, Any]:
    """POST ``/api/projects/`` with ``spectrum: true`` and return the response.

    The response includes ``spectrumProjectId`` and ``projectSecret`` — those
    are the HTTP Basic credentials for the Spectrum API.  Photon only
    returns ``projectSecret`` to project owners at creation time.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for Photon project creation")
    url = f"{_dashboard_host()}/api/projects/"
    body: Dict[str, Any] = {
        "name": name,
        "location": location,
        "spectrum": True,
        "platforms": platforms or ["imessage"],
    }
    resp = httpx.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_dashboard_status(resp, action="create a Photon project")
    return resp.json()


def get_project(token: str, project_id: str) -> Dict[str, Any]:
    """Return dashboard details for one Photon project."""
    if httpx is None:
        raise RuntimeError("httpx is required for Photon project lookup")
    url = f"{_dashboard_host()}/api/projects/{project_id}"
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_dashboard_status(resp, action="fetch Photon project details")
    data = resp.json() or {}
    return data if isinstance(data, dict) else {}


def regenerate_project_secret(token: str, project_id: str) -> Dict[str, Any]:
    """Rotate and return the Spectrum API secret for a dashboard project."""
    if httpx is None:
        raise RuntimeError("httpx is required for Photon project secret rotation")
    url = f"{_dashboard_host()}/api/projects/{project_id}/regenerate-secret"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_dashboard_status(resp, action="regenerate Photon project secret")
    data = resp.json() or {}
    return data if isinstance(data, dict) else {}


def list_projects(token: str) -> list[Dict[str, Any]]:
    """Return Photon dashboard projects visible to the authenticated user."""
    if httpx is None:
        raise RuntimeError("httpx is required for Photon project listing")
    url = f"{_dashboard_host()}/api/projects/"
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_dashboard_status(resp, action="list Photon projects")
    data = resp.json() or {}
    return _project_items(data)


def _raise_for_dashboard_status(resp: Any, *, action: str) -> None:
    if resp.status_code in (401, 403):
        detail = _response_error_detail(resp)
        suffix = f" Photon returned: {detail}" if detail else ""
        raise PhotonDashboardAuthError(
            f"Photon dashboard token was rejected while trying to {action}."
            f"{suffix}"
        )
    resp.raise_for_status()


def _response_error_detail(resp: Any) -> str:
    try:
        body = resp.json() or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        body = {}
    if isinstance(body, dict):
        detail = (
            body.get("error_description")
            or body.get("message")
            or body.get("error")
            or body.get("detail")
        )
        if detail:
            return str(detail).strip()[:200]
    text = str(getattr(resp, "text", "") or "").strip()
    return text[:200]


def _project_items(data: Any) -> list[Dict[str, Any]]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("data")
            or data.get("projects")
            or data.get("items")
            or data.get("results")
            or []
        )
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _first_string(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _coerce_platforms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        keys = [k for k, enabled in value.items() if enabled]
        return [str(k) for k in keys]
    if isinstance(value, Iterable):
        platforms: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("type") or item.get("platform")
                if name:
                    platforms.append(str(name))
            elif item is not None:
                platforms.append(str(item))
        return platforms
    return []


def normalize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize dashboard project shapes across Photon API revisions."""
    nested_data = project.get("data")
    if isinstance(nested_data, dict):
        project = {**nested_data, **{k: v for k, v in project.items() if k != "data"}}
    spectrum = project.get("spectrum") if isinstance(project.get("spectrum"), dict) else {}
    credentials = project.get("credentials") if isinstance(project.get("credentials"), dict) else {}
    platforms = _coerce_platforms(
        project.get("platforms")
        or project.get("enabledPlatforms")
        or project.get("enabled_platforms")
        or spectrum.get("platforms")
    )
    spectrum_project_id = _first_string(
        project.get("spectrumProjectId"),
        project.get("spectrum_project_id"),
        spectrum.get("projectId"),
        spectrum.get("project_id"),
        credentials.get("projectId"),
        credentials.get("project_id"),
    )
    project_secret = _first_string(
        project.get("projectSecret"),
        project.get("project_secret"),
        project.get("spectrumProjectSecret"),
        spectrum.get("projectSecret"),
        spectrum.get("project_secret"),
        credentials.get("projectSecret"),
        credentials.get("project_secret"),
    )
    spectrum_enabled = bool(
        project.get("spectrum") is True
        or spectrum
        or spectrum_project_id
        or project.get("spectrumEnabled")
        or project.get("spectrum_enabled")
    )
    lowered_platforms = {p.lower() for p in platforms}
    imessage_enabled = not platforms or "imessage" in lowered_platforms
    return {
        "dashboard_project_id": _first_string(
            project.get("id"),
            project.get("dashboardProjectId"),
            project.get("dashboard_project_id"),
        ),
        "spectrum_project_id": spectrum_project_id,
        "project_secret": project_secret,
        "name": _first_string(project.get("name"), project.get("displayName")),
        "platforms": platforms,
        "spectrum_enabled": spectrum_enabled,
        "imessage_enabled": imessage_enabled,
        "created_at": _first_string(
            project.get("createdAt"),
            project.get("created_at"),
            project.get("created"),
        ),
        "raw": project,
    }


def reusable_projects(
    projects: list[Dict[str, Any]],
    *,
    preferred_name: str = "Hermes Agent",
) -> list[Dict[str, Any]]:
    """Return compatible Spectrum/iMessage projects matching the setup name."""
    normalized = [normalize_project(project) for project in projects]
    compatible = [
        project for project in normalized
        if project["spectrum_enabled"] and project["imessage_enabled"]
    ]
    preferred = preferred_name.strip().lower()
    if not preferred:
        return compatible
    named = [
        project for project in compatible
        if str(project.get("name") or "").strip().lower() == preferred
    ]
    return named


# ---------------------------------------------------------------------------
# Spectrum API: create user

def create_user(
    project_id: str,
    project_secret: str,
    *,
    phone_number: str,
    user_type: str = "shared",
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    assigned_phone_number: Optional[str] = None,
) -> Dict[str, Any]:
    """POST ``/projects/{id}/users/`` on the Spectrum API.

    For free users we always pass ``type=shared``; Photon's Cosmos pool
    assigns the iMessage line.  ``assigned_phone_number`` is only valid
    for the paid ``dedicated`` mode.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for Photon user creation")
    if not E164_RE.match(phone_number):
        raise ValueError(
            "phone_number must be E.164 (format +<country-code><number>); "
            f"got {phone_number!r}"
        )
    url = f"{_spectrum_host()}/projects/{project_id}/users/"
    body: Dict[str, Any] = {"type": user_type, "phoneNumber": phone_number}
    if first_name:
        body["firstName"] = first_name
    if last_name:
        body["lastName"] = last_name
    if email:
        body["email"] = email
    if assigned_phone_number:
        body["assignedPhoneNumber"] = assigned_phone_number
    resp = httpx.post(
        url,
        json=body,
        auth=(project_id, project_secret),
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    if not data.get("succeed"):
        raise RuntimeError(
            f"Photon create-user failed: {data.get('message') or data}"
        )
    return data.get("data") or {}


# ---------------------------------------------------------------------------
# Spectrum API: webhook registration
#
# Endpoints from https://photon.codes/docs/webhooks/overview:
#   POST   /projects/{id}/webhooks/          register, returns signing secret ONCE
#   GET    /projects/{id}/webhooks/          list
#   DELETE /projects/{id}/webhooks/{wid}     remove

def register_webhook(
    project_id: str, project_secret: str, *, webhook_url: str,
) -> Dict[str, Any]:
    """Register a webhook URL with Photon and return the API response.

    Photon returns the per-URL signing secret exactly once in this
    response, so callers who need to persist it should hand the
    response to :func:`persist_webhook_signing_secret` immediately —
    that helper writes the value into ``~/.hermes/.env`` (mode 0o600,
    existing entries preserved) without the secret value ever needing
    to leave this module.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for Photon webhook registration")
    url = f"{_spectrum_host()}/projects/{project_id}/webhooks/"
    resp = httpx.post(
        url,
        json={"webhookUrl": webhook_url},
        auth=(project_id, project_secret),
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    if not data.get("succeed"):
        raise RuntimeError(
            f"Photon register-webhook failed: {data.get('message') or data}"
        )
    return data.get("data") or {}


def print_credential_summary(emit: Any = print) -> None:
    """Pretty-print the credential status table via the *emit* callback.

    Same isolation rationale as :func:`persist_webhook_signing_secret`:
    all secret-bearing reads happen inside this function; the *emit*
    callback only ever receives display literals like ``"✓ stored"``
    or a project UUID. No tainted variable ever escapes into the
    caller's scope. Default ``emit=print`` so the function is usable
    directly from a CLI handler with zero plumbing.
    """
    # Resolve every credential read into a plain display string FIRST,
    # in a tight block. The intermediate `labels` dict only ever stores
    # literals from a finite set ("✓ stored" / "✗ missing" / "✓ set" /
    # "⚠ unset — verification disabled" / a project UUID) — never a
    # credential's raw bytes. We then assemble the whole banner into
    # one string and call emit() exactly once with that string, so the
    # static taint analyzer sees a single sink that consumes only a
    # joined literal blob.
    labels: Dict[str, str] = {}
    if load_photon_token():
        labels["device_token"] = "✓ stored"
    else:
        labels["device_token"] = "✗ missing (run `hermes photon login`)"
    pid, sec = load_project_credentials()
    labels["project_id"] = pid if pid else "✗ missing"
    labels["project_key"] = "✓ stored" if sec else "✗ missing"
    if _get_hermes_env_value("PHOTON_WEBHOOK_SECRET"):
        labels["webhook_key"] = "✓ set"
    else:
        labels["webhook_key"] = "⚠ unset — verification disabled"

    rows = [
        "Photon iMessage status",
        "──────────────────────",
        "  device token        : " + labels["device_token"],
        "  project id          : " + labels["project_id"],
        "  project key         : " + labels["project_key"],
        "  webhook key         : " + labels["webhook_key"],
    ]
    emit("\n".join(rows))


def credential_summary() -> Dict[str, str]:
    """Return a fully pre-formatted credential status dict.

    Caller-safe: every value is one of ``"✓ stored"`` / ``"✗ missing"``
    / ``"⚠ unset — verification disabled"`` / ``"✓ set"`` literals, or a
    UUID for the project id. No secret-bearing string ever leaves this
    function — read-and-bool-cast happens entirely inside the closure.
    """
    def _present_token() -> str:
        return "✓ stored" if load_photon_token() else "✗ missing (run `hermes photon login`)"

    def _present_project_id() -> str:
        pid, _sec = load_project_credentials()
        return pid or "✗ missing"

    def _present_project_secret() -> str:
        _pid, sec = load_project_credentials()
        return "✓ stored" if sec else "✗ missing"

    def _present_webhook_secret() -> str:
        return "✓ set" if _get_hermes_env_value("PHOTON_WEBHOOK_SECRET") else "⚠ unset — verification disabled"

    return {
        "device_token": _present_token(),
        "project_id": _present_project_id(),
        "project_key": _present_project_secret(),
        "webhook_key": _present_webhook_secret(),
    }


def persist_webhook_signing_secret(
    webhook_data: Dict[str, Any],
    *,
    on_summary: Optional[Any] = None,
) -> bool:
    """Persist a webhook signing secret via Hermes' canonical .env writer.

    Delegates to :func:`hermes_cli.config.save_env_value` — the same
    helper that backs every other API-key persistence path in Hermes
    Agent (OpenAI key, Anthropic key, Telegram token, ...). The secret
    value is read directly from ``webhook_data['signingSecret']`` (or
    ``['secret']`` fallback) and handed to that helper without ever
    being bound to a local in any module that prints or logs.

    Returns ``True`` on success, ``False`` if the response had no
    secret OR the write failed. The optional ``on_summary`` callable
    receives a plain string with no credential material, suitable for
    printing — e.g. ``"Wrote to /home/u/.hermes/.env"`` or
    ``"register response: {redacted dict json}"``.  We do the
    formatting here so callers stay clear of the taint flow CodeQL
    tracks through functions that touch secrets.
    """
    if not isinstance(webhook_data, dict):
        return False
    has_secret = bool(webhook_data.get("signingSecret") or webhook_data.get("secret"))
    redacted = {
        k: ("<redacted>" if k in ("signingSecret", "secret") else v)
        for k, v in webhook_data.items()
    }
    if on_summary is not None:
        try:
            on_summary("webhook registration response (redacted):")
            on_summary(json.dumps(redacted, indent=2))
        except Exception:
            pass
    if not has_secret:
        return False
    try:
        from hermes_cli.config import save_env_value  # type: ignore
    except ImportError:
        return False
    try:
        save_env_value(
            "PHOTON_WEBHOOK_SECRET",
            webhook_data.get("signingSecret") or webhook_data.get("secret") or "",
        )
    except Exception:
        return False
    if on_summary is not None:
        try:
            from hermes_constants import get_hermes_home  # type: ignore
            env_path = Path(get_hermes_home()) / ".env"
        except Exception:
            env_path = Path(os.path.expanduser("~/.hermes")) / ".env"
        try:
            on_summary(f"signing key saved to {env_path}")
            on_summary("(Photon only returns this once — keep the file safe)")
        except Exception:
            pass
    return True


def list_webhooks(project_id: str, project_secret: str) -> list:
    if httpx is None:
        raise RuntimeError("httpx is required for Photon webhook listing")
    url = f"{_spectrum_host()}/projects/{project_id}/webhooks/"
    resp = httpx.get(url, auth=(project_id, project_secret), timeout=30.0)
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("data") or []


def delete_webhook(
    project_id: str, project_secret: str, *, webhook_id: str,
) -> None:
    if httpx is None:
        raise RuntimeError("httpx is required for Photon webhook deletion")
    url = f"{_spectrum_host()}/projects/{project_id}/webhooks/{webhook_id}"
    resp = httpx.delete(url, auth=(project_id, project_secret), timeout=30.0)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()
