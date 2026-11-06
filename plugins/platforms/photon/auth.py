"""Photon 2.0 setup client: device login, project + API key provisioning, shared iMessage line.

Calls go through Photon's own Python SDK (``photonhq-api``, the ``photon`` extra) against
``https://api.photon.codes/v1``. Setup signs the operator in with Photon's device login, or reuses
the project credentials already in the environment, then finds or creates the project, mints a
project API key and assigns the operator's phone number to a shared iMessage line. The gateway
only ever uses the project key; the device-login session exists for setup alone.

Storage: runtime credentials -> ``~/.hermes/.env`` (``PHOTON_PROJECT_ID`` = ``pho_prj_…``,
``PHOTON_API_KEY`` = ``pho_sk_…``); management metadata -> ``auth.json`` under
``credential_pool``: ``photon`` (device-login session), ``photon_project`` (organization, project
and key ids), ``photon_user`` (numbers) and ``photon_legacy`` (Photon Spectrum credentials from
before Photon 2.0, kept for Photon's coming migration tool).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

try:
    from photon_api import ApiError, Photon, PhotonError, TransportError
except ImportError:  # the `photon` extra installs on first use, see ensure_sdk()
    Photon = None

    class PhotonError(Exception):  # type: ignore[no-redef]
        """Placeholder until ensure_sdk() binds the SDK's PhotonError."""

    class ApiError(PhotonError):  # type: ignore[no-redef]
        """Placeholder until ensure_sdk() binds the SDK's ApiError."""

    class TransportError(PhotonError):  # type: ignore[no-redef]
        """Placeholder until ensure_sdk() binds the SDK's TransportError."""


DEFAULT_API_HOST = "https://api.photon.codes"
DASHBOARD_URL = "https://app.beta.photon.codes"
DEFAULT_PROJECT_NAME = "Hermes Agent"
PROJECT_ID_PREFIX = "pho_prj_"
# What the gateway needs: the event stream, message history and sends (events, project), and
# the line mode + assignments for setup and status (platforms). Seats stay with the account.
PROJECT_KEY_PERMISSIONS = ["project:read", "project:write", "events:read", "platforms:read"]
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_POLL_INTERVAL = 5  # RFC 8628 default; Photon's `interval` wins
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_KEY_REJECTED_STATUSES = (401, 403, 404, 410)

LEGACY_NOTICE = (
    "⚠ This Hermes is still set up for Photon Spectrum, the platform before Photon 2.0.\n"
    "  Hermes now runs on Photon 2.0. Create your Photon 2.0 account at " + DASHBOARD_URL + "\n"
    "  and run `hermes photon setup` to connect it. Your Spectrum projects can move to Photon 2.0\n"
    "  once Photon's migration tool ships; setup keeps your Spectrum credentials for it.")


class PhotonAuthError(RuntimeError):
    """A setup step Photon refused for a reason the operator has to fix (shown as-is)."""


# -- auth.json helpers (shares the file with the rest of hermes-agent) ------------

def _auth_json_path() -> Path:
    """The active profile's ``auth.json`` (shared with the rest of hermes-agent)."""
    return get_hermes_home() / "auth.json"


def _load_auth() -> dict[str, Any]:
    path = _auth_json_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("photon: could not read %s: %s", path, e)
        return {}


def _save_auth(data: dict[str, Any]) -> None:
    path = _auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Random per-process temp name (no collisions / pre-planted symlinks), created 0o600
    # atomically via O_EXCL so the bearer token is never world-readable at umask.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:  # fdopen failed before owning the descriptor — nothing else will close it
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    try:
        with fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _pool_first(auth: dict[str, Any], key: str) -> Any:
    """First entry of ``credential_pool.<key>`` (a list), or None."""
    pool = auth.get("credential_pool", {}).get(key) or []
    return pool[0] if isinstance(pool, list) and pool else None


def _store_pool_record(key: str, record: Optional[dict[str, Any]]) -> None:
    """Replace ``credential_pool.<key>`` with ``[record]`` (``[]`` for None) under the
    cross-process lock."""
    from hermes_cli.auth import _auth_store_lock
    with _auth_store_lock():
        auth = _load_auth()
        auth.setdefault("credential_pool", {})[key] = [record] if record else []
        _save_auth(auth)


def _now() -> int:
    return int(time.time())


# -- SDK plumbing -----------------------------------------------------------------

def ensure_sdk() -> bool:
    """Install the ``photon`` extra on first use and bind the SDK names (Slack's pattern)."""
    if Photon is not None:
        return True

    def _import() -> dict[str, Any]:
        from photon_api import ApiError, Photon, PhotonError, TransportError
        return {"ApiError": ApiError, "Photon": Photon, "PhotonError": PhotonError, "TransportError": TransportError}

    from pm.extras import ensure_and_bind
    return ensure_and_bind("photon", _import, globals())


def _api_host() -> str:
    return (os.getenv("PHOTON_API_HOST") or DEFAULT_API_HOST).rstrip("/")


def _photon(token: Optional[str] = None, *, timeout: float = 30.0) -> Any:
    """An SDK client sending ``token`` as the bearer credential (none for the device-login calls)."""
    if not ensure_sdk():
        raise RuntimeError("Photon's Python client (the `photon` extra) could not be installed")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return Photon(base_url=_api_host(), headers=headers, timeout=timeout)


def _rpc(name: str, payload: dict[str, Any]) -> Any:
    """The SDK input model ``name`` built from wire-shaped ``payload``."""
    from photon_api import rpc_generated
    return getattr(rpc_generated, name).model_validate(payload)


def _data(model: Any) -> Any:
    """A response as plain JSON data; drops the SDK's MISSING placeholders for absent fields."""
    return model.model_dump(mode="json", by_alias=True)


def problem_code(exc: BaseException) -> str:
    """The Photon problem ``code`` (or OAuth ``error``) carried by an ApiError, else ``""``."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    return str(body.get("code") or body.get("error") or "")


def _idempotency(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "headers": {"Idempotency-Key": str(uuid.uuid4())}}


# -- Device login (RFC 8628) --------------------------------------------------------

@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int


def request_device_code() -> DeviceCode:
    """Start Photon's device login (the server picks the client, so no client_id is sent)."""
    with _photon() as photon:
        data = _data(photon.auth.device.authorize())
    return DeviceCode(
        device_code=data["device_code"], user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        verification_uri_complete=data.get("verification_uri_complete"),
        expires_in=int(data["expires_in"]), interval=int(data.get("interval") or DEFAULT_POLL_INTERVAL))


def _token_grant(body: dict[str, Any]) -> dict[str, Any]:
    with _photon() as photon:
        return _data(photon.auth.device.token(_rpc("DeviceTokenInput", {"body": body})))


def poll_for_token(
    code: DeviceCode, *, on_pending: Optional[Callable[[], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll until the user approves. ``authorization_pending`` and 5xx keep the interval,
    ``slow_down``/429 add 5 s, ``access_denied``/``expired_token`` abort."""
    deadline = clock() + code.expires_in
    interval = code.interval or DEFAULT_POLL_INTERVAL
    grant = {"grant_type": DEVICE_CODE_GRANT, "device_code": code.device_code}
    while clock() < deadline:
        sleep_fn(interval)
        try:
            return _token_grant(grant)
        except TransportError as e:
            logger.warning("photon: device-token poll failed: %s", e)
        except ApiError as e:
            err = problem_code(e)
            if e.status == 429 or err == "slow_down":
                interval += 5
            elif err in ("access_denied", "expired_token"):
                raise RuntimeError(f"Photon login failed: {err}") from e
            elif err != "authorization_pending" and e.status < 500:
                raise RuntimeError(f"Photon device login error: {err or e.status}") from e
        if on_pending:
            on_pending()
    raise TimeoutError("Photon device login timed out")


def _store_session(token: dict[str, Any]) -> str:
    """Persist a device-login token pair; refresh tokens rotate, so always keep the newest."""
    user = token.get("user") or {}
    _store_pool_record("photon", {
        "access_token": token["access_token"], "refresh_token": token["refresh_token"],
        "expires_at": _now() + int(token.get("expires_in") or 0), "email": user.get("email"),
        "issued_at": _now()})
    return str(token["access_token"])


def login_device_flow(
    *, open_browser: bool = True, on_user_code: Optional[Callable[[DeviceCode], None]] = None,
    on_pending: Optional[Callable[[], None]] = None,
) -> str:
    """Run the device login, persist the session and return its account access token."""
    code = request_device_code()
    if on_user_code:
        on_user_code(code)
    if open_browser:
        import webbrowser
        with contextlib.suppress(webbrowser.Error):
            webbrowser.open(code.verification_uri_complete or code.verification_uri, new=2)
    return _store_session(poll_for_token(code, on_pending=on_pending))


def load_account_token() -> Optional[str]:
    """A live account token from the stored device-login session, refreshed when expired.
    None when there is no session or Photon refuses the refresh: sign in again."""
    session = _pool_first(_load_auth(), "photon") or {}
    if session.get("access_token") and float(session.get("expires_at") or 0) > time.time() + 30:
        return str(session["access_token"])
    refresh = session.get("refresh_token")
    if not refresh:
        return None
    try:
        return _store_session(_token_grant({"grant_type": "refresh_token", "refresh_token": refresh}))
    except ApiError as e:
        if e.status != 400:
            raise
        clear_photon_session()
        return None


def clear_photon_session() -> None:
    """Forget the device-login session (before signing in again)."""
    _store_pool_record("photon", None)


def signed_in_email() -> Optional[str]:
    session = _pool_first(_load_auth(), "photon") or {}
    return session.get("email") if session.get("refresh_token") else None


# -- Organizations and projects -----------------------------------------------------

def list_organizations(token: str) -> list[dict[str, Any]]:
    """The account's organizations, oldest first, each marked ``owned`` when the account owns it."""
    with _photon(token) as photon:
        data = _data(photon.organizations.list_organizations())
    owned = set(data.get("ownedOrganizationIds") or [])
    return [{"organization_id": o["organizationId"], "name": o["name"], "owned": o["organizationId"] in owned}
            for o in data.get("organizations") or []]


def find_project_by_name(token: str, organization_id: str, name: str) -> Optional[str]:
    """Id of the organization's project named ``name`` (case-insensitive), or None."""
    target = name.strip().lower()
    query: dict[str, Any] = {"query": name}
    with _photon(token) as photon:
        while True:
            page = _data(photon.organizations.projects.list(_rpc(
                "ListProjectsInput", {"path": {"organizationId": organization_id}, "query": query})))
            for project in page.get("projects") or []:
                if (project.get("name") or "").strip().lower() == target:
                    return str(project["projectId"])
            if not page.get("nextPageToken"):
                return None
            query["pageToken"] = page["nextPageToken"]


def create_project(token: str, organization_id: str, name: str = DEFAULT_PROJECT_NAME) -> str:
    """Create a project and return its id. Slugs are immutable and globally unique, so it gets
    a random suffix rather than one derived from ``name``."""
    payload = {"path": {"organizationId": organization_id},
               "body": {"name": name, "slug": f"hermes-agent-{uuid.uuid4().hex[:8]}"}}
    with _photon(token) as photon:
        project = _data(photon.organizations.projects.create(_rpc("CreateProjectInput", _idempotency(payload))))
    return str(project["projectId"])


def project_exists(token: str, project_id: str) -> bool:
    """False when Photon reports the project missing or deleted."""
    try:
        with _photon(token) as photon:
            photon.projects.get(_rpc("GetProjectInput", {"path": {"projectId": project_id}}))
    except ApiError as e:
        if e.status in (404, 410):
            return False
        raise
    return True


def project_key_rejected(project_id: str, api_key: str) -> bool:
    """True only when Photon definitively refuses ``api_key`` for ``project_id``; network
    trouble is not a rejection, so it never forces a needless new key."""
    try:
        with _photon(api_key) as photon:
            photon.projects.get(_rpc("GetProjectInput", {"path": {"projectId": project_id}}))
    except ApiError as e:
        return e.status in _KEY_REJECTED_STATUSES
    except TransportError as e:
        logger.debug("photon: could not verify the project key: %s", e)
    return False


def mint_project_key(token: str, project_id: str) -> tuple[str, str]:
    """Create the gateway's project API key; returns ``(api_key_id, secret)``. Photon shows the
    secret only in this response."""
    payload = {"path": {"projectId": project_id},
               "body": {"name": f"Hermes Agent ({socket.gethostname()})"[:128],
                        "permissions": PROJECT_KEY_PERMISSIONS}}
    try:
        with _photon(token) as photon:
            created = _data(photon.projects.create_project_api_key(
                _rpc("CreateProjectApiKeyInput", _idempotency(payload))))
    except ApiError as e:
        if problem_code(e) != "API_KEY_LIMIT_REACHED":
            raise
        raise PhotonAuthError(
            f"this project has reached Photon's API key limit; revoke an unused key at {DASHBOARD_URL} "
            "and run setup again") from e
    return str(created["apiKey"]["apiKeyId"]), str(created["secret"])


def revoke_project_key(token: str, project_id: str, api_key_id: str) -> None:
    payload = {"path": {"projectId": project_id, "apiKeyId": api_key_id}}
    with _photon(token) as photon:
        photon.projects.revoke_project_api_key(_rpc("RevokeProjectApiKeyInput", _idempotency(payload)))


# -- Runtime credentials ----------------------------------------------------------

def load_runtime_credentials() -> tuple[Optional[str], Optional[str]]:
    """``(project_id, api_key)`` the gateway runs with. The process env wins (``.env`` is loaded
    at startup); the non-secret project id falls back to ``auth.json``."""
    project_id = _get_scoped_secret("PHOTON_PROJECT_ID")
    if not (project_id and project_id.startswith(PROJECT_ID_PREFIX)):
        project_id = load_project_record().get("project_id")
    return project_id or None, _get_scoped_secret("PHOTON_API_KEY") or None


def load_project_record() -> dict[str, Any]:
    """The Photon 2.0 ``photon_project`` record (empty for a Spectrum-era record)."""
    entry = _pool_first(_load_auth(), "photon_project") or {}
    return entry if str(entry.get("project_id") or "").startswith(PROJECT_ID_PREFIX) else {}


def store_project_credentials(
    *, project_id: str, api_key: str, organization_id: Optional[str] = None,
    api_key_id: Optional[str] = None, name: Optional[str] = None) -> None:
    """Runtime credentials -> ``.env``; ids -> ``auth.json``. The key itself stays out of
    ``auth.json``: ``.env`` is the one place secrets live."""
    record: dict[str, Any] = {"project_id": project_id, "issued_at": _now()}
    record.update({k: v for k, v in (
        ("organization_id", organization_id), ("api_key_id", api_key_id), ("name", name)) if v})
    _store_pool_record("photon_project", record)
    from hermes_cli.config import save_env_value
    save_env_value("PHOTON_PROJECT_ID", project_id)
    save_env_value("PHOTON_API_KEY", api_key)


# -- Photon Spectrum (pre-2.0) credentials --------------------------------------------

def load_project_credentials() -> tuple[Optional[str], Optional[str]]:
    """Spectrum-era ``(spectrum_project_id, project_secret)``: process env wins, then the
    pre-2.0 ``photon_project`` record in ``auth.json``."""
    env_id = _get_scoped_secret("PHOTON_PROJECT_ID")
    env_sec = _get_scoped_secret("PHOTON_PROJECT_SECRET")
    if env_id and env_sec:
        return env_id, env_sec
    entry = _pool_first(_load_auth(), "photon_project")
    if entry is None:
        return env_id, env_sec
    # back-compat: old records used "project_id" for the spectrum id
    return env_id or entry.get("spectrum_project_id") or entry.get("project_id"), env_sec or entry.get("project_secret")


def has_legacy_credentials() -> bool:
    """True when this home still carries Photon Spectrum credentials."""
    spectrum_id, secret = load_project_credentials()
    return bool(secret) or bool(spectrum_id and not spectrum_id.startswith(PROJECT_ID_PREFIX))


def archive_legacy_credentials() -> bool:
    """Move Spectrum credentials out of the runtime slots into ``credential_pool.photon_legacy``,
    where Photon's migration tool can pick them up. True when anything was archived."""
    if not has_legacy_credentials():
        return False
    env_id = _get_scoped_secret("PHOTON_PROJECT_ID")
    entry = _pool_first(_load_auth(), "photon_project") or {}
    # A Photon 2.0 id already in PHOTON_PROJECT_ID must not hide the Spectrum id kept in auth.json.
    spectrum_id = next((str(v) for v in (env_id, entry.get("spectrum_project_id"), entry.get("project_id"))
                        if v and not str(v).startswith(PROJECT_ID_PREFIX)), None)
    secret = _get_scoped_secret("PHOTON_PROJECT_SECRET") or entry.get("project_secret")
    record: dict[str, Any] = {"archived_at": _now()}
    record.update({k: v for k, v in (("spectrum_project_id", spectrum_id), ("project_secret", secret)) if v})
    _store_pool_record("photon_legacy", record)
    if entry.get("project_secret"):
        _store_pool_record("photon_project", None)
    from hermes_cli.config import remove_env_value
    remove_env_value("PHOTON_PROJECT_SECRET")
    if env_id and not env_id.startswith(PROJECT_ID_PREFIX):
        remove_env_value("PHOTON_PROJECT_ID")
    return True


# -- Shared iMessage line -------------------------------------------------------------

def _normalize_phone(phone: str) -> str:
    """Reduce a phone string to ``+`` and digits for comparison."""
    return re.sub(r"[^\d+]", "", phone or "")


def get_imessage_mode(token: str, project_id: str) -> str:
    """``shared`` (pooled lines, one assignment per user) or ``dedicated``."""
    with _photon(token) as photon:
        settings = _data(photon.projects.platforms.get_imessage(
            _rpc("GetProjectImessagePlatformInput", {"path": {"id": project_id}})))
    return str(settings.get("mode") or "")


def _assignment(record: dict[str, Any]) -> dict[str, Any]:
    return {"assignment_id": record["assignmentId"], "handle": record["handle"],
            "assigned_phone_number": record["assignedPhoneNumber"], "created_at": record["createdAt"]}


def find_line_assignment(token: str, project_id: str, phone: str) -> Optional[dict[str, Any]]:
    """The active shared-line assignment for ``phone``, or None."""
    target = _normalize_phone(phone)
    query: dict[str, Any] = {"pageSize": 100}
    with _photon(token) as photon:
        while True:
            page = _data(photon.projects.platforms.imessage.assignments.list(_rpc(
                "ListSharedLineAssignmentsInput", {"path": {"id": project_id}, "query": query})))
            for record in page.get("assignments") or []:
                if _normalize_phone(record.get("handle") or "") == target:
                    return _assignment(record)
            if not page.get("nextPageToken"):
                return None
            query["pageToken"] = page["nextPageToken"]


def ensure_line_assignment(
    token: str, project_id: str, *, phone: str, first_name: Optional[str] = None,
    last_name: Optional[str] = None, email: Optional[str] = None) -> tuple[dict[str, Any], bool]:
    """Assign ``phone`` to a shared iMessage line unless it already has one: ``(assignment, created)``.
    An ``email`` makes Photon send the user an invite."""
    if not E164_RE.match(phone):
        raise ValueError(f"phone number must be E.164 (e.g. +15551234567); got {phone!r}")
    existing = find_line_assignment(token, project_id, phone)
    if existing is not None:
        return existing, False
    body: dict[str, Any] = {"handle": phone}
    body.update({k: v for k, v in (("firstName", first_name), ("lastName", last_name), ("email", email)) if v})
    try:
        with _photon(token) as photon:
            created = _data(photon.projects.platforms.imessage.assignments.create(_rpc(
                "CreateSharedLineAssignmentInput", _idempotency({"path": {"id": project_id}, "body": body}))))
    except ApiError as e:
        raced = find_line_assignment(token, project_id, phone) if problem_code(e) == "ASSIGNMENT_EXISTS" else None
        if raced is None:
            raise
        return raced, False
    return _assignment(created), True


def has_texted_line(api_key: str, project_id: str, phone: str, since: str, *, timeout: float = 30.0) -> bool:
    """True once ``phone`` has messaged the project since ``since`` (its assignment's creation).
    A shared line can only message people who have texted it, so this is when routing goes live."""
    target = _normalize_phone(phone)
    query: dict[str, Any] = {"createdSince": since, "direction": "received", "platform": "imessage",
                             "order": "asc", "pageSize": 100}
    with _photon(api_key, timeout=timeout) as photon:
        while True:
            page = _data(photon.projects.list_messages(
                _rpc("ListMessagesInput", {"path": {"id": project_id}, "query": query})))
            if any(_normalize_phone((m.get("sentBy") or {}).get("handle") or "") == target
                   for m in page.get("items") or []):
                return True
            if not page.get("nextPageToken"):
                return False
            query["pageToken"] = page["nextPageToken"]


def wait_for_first_text(
    api_key: str, project_id: str, phone: str, since: str, *, timeout_s: float = 300.0,
    interval_s: float = 5.0, on_waiting: Optional[Callable[[int], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """Poll until ``phone`` texts the line, bounded by both an attempt cap and a wall-clock
    deadline (each list call can take its 30 s HTTP timeout). A failed call is a missed poll."""
    deadline = clock() + timeout_s
    max_attempts = max(1, int(timeout_s / interval_s))
    for attempt in range(1, max_attempts + 1):
        try:
            if has_texted_line(api_key, project_id, phone, since):
                return True
        except PhotonError as exc:
            logger.debug("photon: first-text poll failed (attempt %d): %s", attempt, exc)
        if on_waiting is not None:
            on_waiting(attempt)
        if attempt == max_attempts or clock() + interval_s > deadline:
            break
        sleep_fn(interval_s)
    return False


# -- Operator numbers (display metadata) ---------------------------------------------

def store_user_numbers(
    *, phone_number: Optional[str] = None, assigned_phone_number: Optional[str] = None,
    assigned_at: Optional[str] = None, project_id: Optional[str] = None) -> None:
    """Persist non-secret operator numbers for offline ``status`` output."""
    if not phone_number and not assigned_phone_number:
        return
    record: dict[str, Any] = {"issued_at": _now()}
    record.update({k: v for k, v in (
        ("phone_number", phone_number), ("assigned_phone_number", assigned_phone_number),
        ("assigned_at", assigned_at), ("project_id", project_id)) if v})
    _store_pool_record("photon_user", record)


def _user_record() -> dict[str, Any]:
    entry = _pool_first(_load_auth(), "photon_user")
    return entry if isinstance(entry, dict) else {}


def load_user_numbers() -> tuple[Optional[str], Optional[str]]:
    """``(operator_phone_number, assigned_phone_number)`` for status."""
    entry = _user_record()
    phone = entry.get("phone_number") or entry.get("phoneNumber")
    assigned = entry.get("assigned_phone_number") or entry.get("assignedPhoneNumber")
    return (str(phone) if phone else _configured_operator_phone(), str(assigned) if assigned else None)


def refresh_user_numbers() -> tuple[Optional[str], Optional[str]]:
    """Re-read the operator's assignment from Photon with the runtime key, provisioning nothing."""
    phone, assigned = load_user_numbers()
    project_id, api_key = load_runtime_credentials()
    if not (phone and project_id and api_key):
        return phone, assigned
    found = find_line_assignment(api_key, project_id, phone)
    if found:
        assigned = found["assigned_phone_number"]
        store_user_numbers(phone_number=phone, assigned_phone_number=assigned,
                           assigned_at=found["created_at"], project_id=project_id)
    return phone, assigned


def _configured_operator_phone() -> Optional[str]:
    """Infer the operator's E.164 number from existing Photon env settings."""
    home = _normalize_phone(_get_config_env_value("PHOTON_HOME_CHANNEL") or "")
    if home and E164_RE.match(home):
        return home
    allowed = _get_config_env_value("PHOTON_ALLOWED_USERS") or ""
    candidates = [n for n in map(_normalize_phone, re.split(r"[,\s]+", allowed)) if E164_RE.match(n)]
    return candidates[0] if len(candidates) == 1 else None


def _get_config_env_value(key: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        return os.getenv(key)
    return get_env_value(key)


# -- Credential status (display-only — never emits raw secret material) ---------------

def print_credential_summary(emit: Any = print) -> None:
    """Pretty-print the credential status table via *emit*. Every secret-bearing read is
    reduced to a display literal here; the callback only ever receives the assembled
    banner, so no tainted value escapes to the caller."""
    project_id, api_key = load_runtime_credentials()
    phone, assigned = load_user_numbers()
    email = signed_in_email()
    rows = [
        "Photon iMessage status",
        "──────────────────────",
        "  Photon account      : " + (f"✓ signed in as {email}" if email else "– not signed in (only setup needs it)"),
        "  project id          : " + (project_id if project_id else "✗ missing (run `hermes photon setup`)"),
        "  API key             : " + ("✓ stored" if api_key else "✗ missing (run `hermes photon setup`)"),
        "  my number           : " + (phone if phone else "✗ missing (run `hermes photon setup --phone ...`)"),
        "  assigned number     : " + (assigned if assigned else "✗ missing (run `hermes photon setup`)")]
    if has_legacy_credentials():
        rows.append(LEGACY_NOTICE)
    emit("\n".join(rows))


_STATUS_TIMEOUT_S = 5.0  # `photon status` stays quick when the API is unreachable


def print_routing_status(emit: Any = print) -> None:
    """Live routing row for ``status``: a shared line can only reach a number that has texted
    it since its assignment. Verified against the API, not local markers."""
    phone, assigned = load_user_numbers()
    project_id, api_key = load_runtime_credentials()
    since = _user_record().get("assigned_at")
    if not (phone and project_id and api_key and since):
        return
    try:
        texted = has_texted_line(api_key, project_id, phone, since, timeout=_STATUS_TIMEOUT_S)
    except (PhotonError, RuntimeError) as e:
        emit(f"  routing             : ? could not verify ({str(e) or type(e).__name__})")
        return
    if texted:
        emit("  routing             : ✓ live (verified: you've texted your line)")
    else:
        emit(f"  routing             : ✗ not live — text {assigned or 'your assigned line'} once from {phone}, then re-check")
