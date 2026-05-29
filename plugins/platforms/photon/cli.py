"""
``hermes photon ...`` CLI subcommands — registered by the plugin via
``ctx.register_cli_command()``.

Subcommands:

    login              run the device-code OAuth flow
    quick-setup        reconcile Photon setup and prove runtime readiness
    allow-phone        authorize another E.164 sender for Photon gateway use
    status             show Photon setup/runtime invariant state
    reset              clear local Photon state, or --all for owned remote cleanup
    webhook register   register the local webhook URL with Photon
    webhook list       list registered webhooks
    webhook delete     delete a webhook by id
    webhook tunnel     manage the local Cloudflare Quick Tunnel
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import urllib.error
import urllib.request

from . import auth as photon_auth
from . import tunnel as photon_tunnel

_SIDECAR_DIR = Path(__file__).parent / "sidecar"
_MIN_SPECTRUM_TS_VERSION = (1, 7, 2)
_DEFAULT_SIDECAR_PORT = 8789
_DEFAULT_SIDECAR_BIND = "127.0.0.1"
_PHONE_FORMAT = "+<country-code><number>"
_PHONE_ARG_PLACEHOLDER = f"'{_PHONE_FORMAT}'"
_PHOTON_RUNTIME_RESET_ENV_KEYS = (
    "PHOTON_PROJECT_ID",
    "PHOTON_PROJECT_SECRET",
    "PHOTON_WEBHOOK_SECRET",
    "PHOTON_WEBHOOK_PUBLIC_URL",
)
_PHOTON_ALL_RESET_ENV_KEYS = (
    *_PHOTON_RUNTIME_RESET_ENV_KEYS,
    "PHOTON_DASHBOARD_TOKEN",
    "PHOTON_ALLOWED_USERS",
    "PHOTON_ALLOW_ALL_USERS",
    "PHOTON_HOME_CHANNEL",
    "PHOTON_HOME_CHANNEL_NAME",
)
_PHOTON_GATEWAY_ENV_KEYS = (
    *_PHOTON_ALL_RESET_ENV_KEYS,
    "PHOTON_WEBHOOK_TUNNEL_AUTOSTART",
    "PHOTON_WEBHOOK_TUNNEL_STOP_ON_DISCONNECT",
    "PHOTON_WEBHOOK_PORT",
    "PHOTON_WEBHOOK_PATH",
    "PHOTON_WEBHOOK_BIND",
    "PHOTON_SIDECAR_PORT",
    "PHOTON_SIDECAR_AUTOSTART",
    "PHOTON_NODE_BIN",
    "PHOTON_API_HOST",
    "PHOTON_DASHBOARD_HOST",
    "PHOTON_HOME_CHANNEL_THREAD_ID",
)


@dataclass
class _SetupOutcome:
    returncode: int = 0
    project_name: str = "Hermes Agent"
    operator_phone: Optional[str] = None
    assigned_phone_number: Optional[str] = None

    def fail(self, code: int = 1) -> "_SetupOutcome":
        self.returncode = code
        return self


@dataclass
class _HealthResult:
    ok: bool
    url: str
    detail: str
    status: Optional[int] = None


@dataclass
class _WebhookEnsureResult:
    hooks: list
    registered: bool
    secret_changed: bool = False
    public_url_changed: bool = False


@dataclass
class _PhotonSetupContext:
    args: argparse.Namespace
    hermes_home: Path
    env_path: Path
    project_name: str
    webhook_port: int
    webhook_path: str
    project_id: str = ""
    project_secret: str = ""
    dashboard_project_name: str = ""
    operator_phone: Optional[str] = None
    assigned_phone_number: Optional[str] = None
    webhook_url: str = ""
    registered_hooks: Optional[list] = None
    runtime_secrets_changed: bool = False
    gateway_started: bool = False
    local_health: Optional[_HealthResult] = None
    public_health: Optional[tuple[bool, str]] = None
    verbose: bool = False
    log_offsets: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "_PhotonSetupContext":
        hermes_home = photon_tunnel.hermes_home()
        return cls(
            args=args,
            hermes_home=hermes_home,
            env_path=photon_auth._env_path(),
            project_name=_setup_project_name(args),
            webhook_port=photon_tunnel.webhook_port(),
            webhook_path=photon_tunnel.webhook_path(),
            verbose=bool(getattr(args, "verbose", False)),
        )

    def outcome(self) -> _SetupOutcome:
        return _SetupOutcome(
            project_name=self.dashboard_project_name or self.project_name,
            operator_phone=self.operator_phone,
            assigned_phone_number=self.assigned_phone_number,
        )


class _FailedInvariant(RuntimeError):
    def __init__(
        self,
        *,
        step: str,
        summary: str,
        expected: str,
        observed: Any,
        evidence: dict[str, Any],
        repair: str,
        logs: Optional[dict[str, list[str]]] = None,
    ) -> None:
        super().__init__(summary)
        self.step = step
        self.summary = summary
        self.expected = expected
        self.observed = observed
        self.evidence = evidence
        self.repair = repair
        self.logs = logs or {}


# ---------------------------------------------------------------------------
# argparse wiring

def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire up `hermes photon ...` subcommands."""
    subs = parser.add_subparsers(dest="photon_command", required=False)

    p_login = subs.add_parser("login", help="Authenticate with Photon (device flow)")
    p_login.add_argument("--no-browser", action="store_true",
                         help="Don't try to open a browser; print the URL only")
    p_login.add_argument("--debug-auth", action="store_true",
                         help="Print sanitized Photon auth exchange diagnostics")

    p_quick = subs.add_parser(
        "quick-setup",
        help="Reconcile Photon setup and prove runtime readiness",
    )
    p_quick.add_argument("--project-name", default=None, help="Project name (default: 'Hermes Agent')")
    p_quick.add_argument("--phone", default=None, help=f"Your E.164 phone number (format: {_PHONE_FORMAT})")
    p_quick.add_argument("--first-name", default=None)
    p_quick.add_argument("--last-name", default=None)
    p_quick.add_argument("--email", default=None)
    p_quick.add_argument("--no-browser", action="store_true")
    p_quick.add_argument("--new-project", action="store_true",
                         help="Create a new Photon dashboard project instead of adopting an existing one")
    p_quick.add_argument("--skip-sidecar-install", action="store_true",
                         help="Skip `npm install` inside the sidecar directory")
    p_quick.add_argument("-v", "--verbose", action="store_true",
                         help="Stream existing gateway/Photon logs while setup waits")

    p_allow = subs.add_parser(
        "allow-phone",
        help="Allow a phone number to control Hermes over Photon",
    )
    p_allow.add_argument("phone", help=f"E.164 phone number (format: {_PHONE_FORMAT})")

    subs.add_parser("status", help="Show Photon setup/runtime invariant state")
    p_reset = subs.add_parser("reset", help="Reset Photon setup state")
    p_reset.add_argument(
        "--all",
        action="store_true",
        help="After confirmation, delete owned Photon webhooks and clear auth state",
    )
    p_reset.add_argument(
        "scope",
        nargs="?",
        choices=("all",),
        metavar="all",
        help="Compatibility alias for --all",
    )

    p_projects = subs.add_parser("projects", help="List or select Photon projects")
    project_subs = p_projects.add_subparsers(dest="photon_projects_command", required=True)
    project_subs.add_parser("list", help="List Photon dashboard projects")
    p_project_select = project_subs.add_parser("select", help="Bind Hermes to an existing Photon project")
    p_project_select.add_argument("project_id", help="Dashboard or Spectrum project id")

    p_hook = subs.add_parser("webhook", help="Manage Photon webhook registrations")
    hook_subs = p_hook.add_subparsers(dest="photon_webhook_command", required=True)
    p_hook_reg = hook_subs.add_parser("register", help="Register a webhook URL")
    p_hook_reg.add_argument("url", help="Publicly reachable URL Photon should POST to")
    hook_subs.add_parser("list", help="List registered webhooks for the current project")
    p_hook_del = hook_subs.add_parser("delete", help="Delete a webhook by id")
    p_hook_del.add_argument("webhook_id")
    p_tunnel = hook_subs.add_parser("tunnel", help="Manage a local Cloudflare Quick Tunnel")
    tunnel_subs = p_tunnel.add_subparsers(dest="photon_tunnel_command", required=True)
    tunnel_subs.add_parser("start", help="Start tunnel and register its Photon webhook")
    tunnel_subs.add_parser("status", help="Show managed tunnel status")
    tunnel_subs.add_parser("stop", help="Stop the managed tunnel")
    tunnel_subs.add_parser("logs", help="Show recent cloudflared tunnel logs")

    parser.set_defaults(func=dispatch)


# ---------------------------------------------------------------------------
# Dispatch

def dispatch(args: argparse.Namespace) -> int:
    sub = getattr(args, "photon_command", None)
    if sub is None:
        # No subcommand given — show status by default.
        return _cmd_status(args)
    if sub == "login":
        return _cmd_login(args)
    if sub == "quick-setup":
        return _cmd_quick_setup(args)
    if sub == "allow-phone":
        return _cmd_allow_phone(args)
    if sub == "status":
        return _cmd_status(args)
    if sub == "reset":
        return _cmd_reset(args)
    if sub == "projects":
        return _cmd_projects(args)
    if sub == "webhook":
        return _cmd_webhook(args)
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Subcommand handlers

def _cmd_login(args: argparse.Namespace) -> int:
    def _print_code(code):
        target = code.verification_uri_complete or code.verification_uri
        print()
        print("┌─ Photon device login ────────────────────────────────────────")
        print(f"│  Open this URL:  {target}")
        print(f"│  Enter the code: {code.user_code}")
        print("│  (waiting for approval — Ctrl-C to cancel)")
        print("└──────────────────────────────────────────────────────────────")
        print()

    try:
        token = photon_auth.login_device_flow(
            open_browser=not args.no_browser,
            on_user_code=_print_code,
            on_debug=(
                _print_login_auth_debug
                if getattr(args, "debug_auth", False)
                else None
            ),
        )
    except photon_auth.PhotonDashboardAuthError as e:
        if not getattr(args, "debug_auth", False):
            print(
                "For sanitized endpoint diagnostics, retry with "
                "`hermes photon login --debug-auth`.",
                file=sys.stderr,
            )
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    # Don't print any portion of the token — even a prefix can help a
    # shoulder-surfer or accidentally leak into a screen recording.
    _ = token
    print(f"✓ logged in — token saved to {photon_auth._env_path()}")
    return 0


def _cmd_quick_setup(args: argparse.Namespace) -> int:
    print("Photon quick setup")
    print("──────────────────")

    ctx = _PhotonSetupContext.from_args(args)
    setattr(args, "auto_create_project", True)
    _print_quick_setup_log_paths(ctx)
    _init_log_offsets(ctx)

    try:
        with photon_auth.setup_lock():
            _run_quick_setup_reconciler(ctx)
    except TimeoutError as e:
        failure = _failed_invariant(
            ctx,
            step="setup lock",
            summary="another Photon setup process is already running",
            expected="exclusive access to Photon setup state",
            observed=str(e),
            repair="wait for the other setup to finish, then rerun quick-setup",
        )
        _finalize_failed_invariant_logs(failure, ctx)
        _print_failed_invariant(failure)
        return 1
    except _FailedInvariant as e:
        _finalize_failed_invariant_logs(e, ctx)
        _print_failed_invariant(e)
        return 1
    except Exception as e:
        failure = _failed_invariant(
            ctx,
            step="quick setup",
            summary="unexpected Photon quick-setup failure",
            expected="all Photon runtime invariants reconciled",
            observed=f"{type(e).__name__}: {e}",
            repair="rerun with `hermes photon status`; if it repeats, inspect IMPLEMENTATION_ERRORS.md and gateway logs",
        )
        _finalize_failed_invariant_logs(failure, ctx)
        _print_failed_invariant(failure)
        return 1

    print()
    _print_quick_setup_reconciled(ctx)
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    reset_all = bool(
        getattr(args, "all", False)
        or getattr(args, "scope", None) == "all"
    )
    hermes_home = photon_tunnel.hermes_home()
    env_path = photon_auth._env_path()
    project_id, project_secret = photon_auth.load_project_credentials()

    if reset_all and not _confirm_reset_all(hermes_home, project_id or ""):
        print("Photon reset aborted.")
        return 1

    print("Photon reset")
    print("────────────")
    print(f"  Hermes home : {hermes_home}")
    print(f"  env path    : {env_path}")
    if project_id:
        print(f"  project     : {project_id}")
    else:
        print("  project     : ✗ none configured")

    stop_result = photon_tunnel.stop()
    print(f"  tunnel      : {stop_result.get('message') or 'checked'}")

    if reset_all:
        if not _delete_owned_webhooks_for_reset(project_id or "", project_secret or ""):
            print(
                "Photon reset stopped before clearing local state so owned webhook "
                "records are retained.",
                file=sys.stderr,
            )
            return 1
        _clear_photon_env_keys(_PHOTON_ALL_RESET_ENV_KEYS)
        _clear_active_home_claim()
        _clear_tunnel_state(preserve_owned_webhooks=False)
        print("  auth        : dashboard token removed if it was present")
        print("Photon reset complete.")
        return 0

    _clear_photon_env_keys(_PHOTON_RUNTIME_RESET_ENV_KEYS)
    _clear_active_home_claim()
    _clear_tunnel_state(preserve_owned_webhooks=True)
    print("  auth        : dashboard token kept")
    print("Photon local reset complete.")
    print("  Remote Photon webhooks were not deleted.")
    return 0


def _confirm_reset_all(hermes_home: Path, project_id: str) -> bool:
    print(_reset_all_confirmation_text(hermes_home, project_id))
    try:
        answer = input().strip()
    except EOFError:
        return False
    return answer == "PHOTON"


def _reset_all_confirmation_text(hermes_home: Path, project_id: str) -> str:
    project_label = project_id or "(none configured)"
    return "\n".join([
        "This will reset Photon for Hermes home:",
        f"  {hermes_home}",
        "",
        "Project:",
        f"  {project_label}",
        "",
        (
            "It may delete owned Photon webhooks, clear local Photon credentials, "
            "stop the managed tunnel, and remove the dashboard token."
        ),
        (
            "It will not delete unowned webhooks or another Hermes home's "
            "gateway state."
        ),
        "",
        "Type PHOTON to continue:",
    ])


def _delete_owned_webhooks_for_reset(project_id: str, project_secret: str) -> bool:
    owned_ids = photon_tunnel.owned_webhook_ids()
    if not owned_ids:
        print("  webhooks    : no owned Photon webhooks recorded")
        return True

    if not (project_id and project_secret):
        print(
            "  webhooks    : cannot delete owned webhooks; missing project credentials",
            file=sys.stderr,
        )
        return False

    try:
        hooks = photon_auth.list_webhooks(project_id, project_secret)
    except Exception as e:
        print(f"  webhooks    : could not list Photon webhooks: {e}", file=sys.stderr)
        return False

    hooks_by_id = {_webhook_id(hook): hook for hook in hooks if _webhook_id(hook)}
    deleted = 0
    missing = 0
    for webhook_id in sorted(owned_ids):
        if webhook_id not in hooks_by_id:
            photon_tunnel.forget_owned_webhook(webhook_id)
            missing += 1
            continue
        try:
            photon_auth.delete_webhook(
                project_id,
                project_secret,
                webhook_id=webhook_id,
            )
        except Exception as e:
            print(
                f"  webhooks    : could not delete owned webhook {webhook_id}: {e}",
                file=sys.stderr,
            )
            return False
        photon_tunnel.forget_owned_webhook(webhook_id)
        deleted += 1

    detail = f"deleted {deleted} owned Photon webhook"
    if deleted != 1:
        detail += "s"
    if missing:
        detail += f"; forgot {missing} missing owned id"
        if missing != 1:
            detail += "s"
    print(f"  webhooks    : {detail}")
    return True


def _clear_photon_env_keys(keys: tuple[str, ...]) -> None:
    removed = [key for key in keys if _remove_env_value(key)]
    if removed:
        print(f"  env         : removed {', '.join(removed)}")
    else:
        print("  env         : no Photon env keys needed removal")


def _clear_active_home_claim() -> None:
    mismatch = photon_tunnel.active_home_mismatch()
    if mismatch:
        owner, current = mismatch
        print(
            "  owner claim : kept; active-home belongs to another Hermes home "
            f"({owner}, current {current})"
        )
        return

    path = photon_tunnel.active_home_path()
    try:
        if path.exists():
            path.unlink()
            print(f"  owner claim : removed {path}")
        else:
            print("  owner claim : none recorded")
    except OSError as e:
        print(f"  owner claim : could not remove {path}: {e}", file=sys.stderr)


def _clear_tunnel_state(*, preserve_owned_webhooks: bool) -> None:
    path = photon_tunnel.state_path()
    state = photon_tunnel.load_state()
    owned = state.get("owned_webhooks") if isinstance(state, dict) else None
    if preserve_owned_webhooks and owned:
        photon_tunnel.save_state({"owned_webhooks": owned})
        print("  state       : cleared tunnel runtime state; kept owned webhook records")
        return

    try:
        if path.exists():
            path.unlink()
            print(f"  state       : removed {path}")
        else:
            print("  state       : no tunnel state file")
    except OSError as e:
        print(f"  state       : could not remove {path}: {e}", file=sys.stderr)


def _run_quick_setup_reconciler(ctx: _PhotonSetupContext) -> None:
    token = _ensure_dashboard_auth(ctx)
    _ensure_spectrum_project(ctx, token)
    _ensure_active_home_available(ctx)
    _ensure_operator_phone(ctx)
    _ensure_sidecar_ready(ctx)
    _ensure_sidecar_port_available(ctx)
    _ensure_public_webhook_path(ctx, wait_for_health=False)
    webhook_result = _ensure_current_webhook_registered(ctx)
    _record_webhook_runtime_changes(ctx, webhook_result)
    _ensure_photon_gateway_platform_enabled(ctx)
    _restart_gateway_if_runtime_secrets_changed(
        ctx,
        reason="Photon webhook state changed",
    )
    _ensure_gateway_local_runtime(ctx)
    ctx.runtime_secrets_changed = False
    _wait_for_public_health_with_managed_repair(
        ctx,
        reason="post-webhook verification",
    )
    _wait_for_photon_connected(ctx)


def _ensure_dashboard_auth(ctx: _PhotonSetupContext) -> str:
    print("[auth] Validating Photon dashboard login...")
    token = photon_auth.load_photon_token()
    if token:
        try:
            photon_auth.validate_photon_token(token)
            print("  ✓ dashboard token is valid for Photon project APIs")
            return token
        except photon_auth.PhotonDashboardAuthError:
            photon_auth.clear_photon_token()
            print("  saved dashboard token is invalid; running device login")
            token = None
        except Exception as e:
            if _http_status(e) in {401, 403}:
                photon_auth.clear_photon_token()
                print("  saved dashboard token was rejected; running device login")
                token = None
            else:
                raise _failed_invariant(
                    ctx,
                    step="dashboard auth",
                    summary="could not validate Photon dashboard token",
                    expected="saved token can access Photon project APIs",
                    observed=f"{type(e).__name__}: {e}",
                    evidence={"dashboard_host": _dashboard_url().rstrip("/")},
                    repair="check network access to Photon, then rerun quick-setup",
                ) from e
        if token:
            return token
    else:
        print("  no dashboard token found; running device login")

    rc = _cmd_login(ctx.args)
    if rc != 0:
        raise _failed_invariant(
            ctx,
            step="dashboard auth",
            summary="Photon device login did not complete",
            expected="device login stores a dashboard API token",
            observed=f"login command exited with {rc}",
            evidence={"dashboard_host": _dashboard_url().rstrip("/")},
            repair="complete `hermes photon login`, then rerun quick-setup",
        )
    token = photon_auth.load_photon_token()
    if not token:
        raise _failed_invariant(
            ctx,
            step="dashboard auth",
            summary="Photon login completed but no token was saved",
            expected="PHOTON_DASHBOARD_TOKEN stored in Hermes env",
            observed="missing PHOTON_DASHBOARD_TOKEN",
            repair=f"inspect env file permissions at {ctx.env_path}",
        )
    try:
        photon_auth.validate_photon_token(token)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="dashboard auth",
            summary="new Photon dashboard token is not valid for project APIs",
            expected="device login returns a project-valid dashboard token",
            observed=f"{type(e).__name__}: {e}",
            evidence={"dashboard_host": _dashboard_url().rstrip("/")},
            repair="retry login; if it repeats, Photon must return or accept a project API bearer token",
        ) from e
    print("  ✓ dashboard token is valid for Photon project APIs")
    return token


def _ensure_spectrum_project(ctx: _PhotonSetupContext, token: str) -> None:
    print("[project] Reconciling Photon Spectrum project...")
    existing_id, existing_secret = photon_auth.load_project_credentials()
    if existing_id and existing_secret and not getattr(ctx.args, "new_project", False):
        try:
            hooks = photon_auth.list_webhooks(existing_id, existing_secret)
        except Exception as e:
            if _http_status(e) == 401:
                print("  stored Spectrum credentials were rejected; clearing cached project state")
                _clear_local_project_runtime_state()
            else:
                raise _failed_invariant(
                    ctx,
                    step="Spectrum credentials",
                    summary="stored Spectrum credentials could not be validated",
                    expected="PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET can call Spectrum APIs",
                    observed=f"{type(e).__name__}: {e}",
                    evidence={"project_id": existing_id},
                    repair="check network access to Photon Spectrum, then rerun quick-setup",
                ) from e
        else:
            ctx.project_id = existing_id
            ctx.project_secret = existing_secret
            ctx.registered_hooks = hooks
            _ensure_dashboard_project_maps_to_spectrum(ctx, token)
            print("  ✓ stored Spectrum credentials validated")
            return

    try:
        project_id, project_secret = _resolve_setup_project(
            ctx.args,
            token,
            total_steps=16,
        )
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="project selection",
            summary="could not resolve a compatible Photon project",
            expected="exactly one compatible project adopted or one new project created",
            observed=f"{type(e).__name__}: {e}",
            repair="run `hermes photon projects list` to inspect available projects",
        ) from e
    if not (project_id and project_secret):
        raise _failed_invariant(
            ctx,
            step="project selection",
            summary="no compatible Photon Spectrum project was selected",
            expected="one compatible Spectrum/iMessage project",
            observed="project id or secret missing after project reconciliation",
            repair="select one project with `hermes photon projects select <id>` or rerun with `--new-project`",
        )

    try:
        hooks = photon_auth.list_webhooks(project_id, project_secret)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="Spectrum credentials",
            summary="newly selected Spectrum credentials failed validation",
            expected="selected project credentials can call Spectrum APIs",
            observed=f"{type(e).__name__}: {e}",
            evidence={"project_id": project_id, "http_status": _http_status(e)},
            repair="select a different project or rerun quick-setup with `--new-project`",
        ) from e

    ctx.project_id = project_id
    ctx.project_secret = project_secret
    ctx.registered_hooks = hooks
    _ensure_dashboard_project_maps_to_spectrum(ctx, token)
    print("  ✓ Spectrum credentials validated")


def _ensure_dashboard_project_maps_to_spectrum(
    ctx: _PhotonSetupContext,
    token: str,
) -> dict[str, Any]:
    """Verify the logged-in dashboard account can see ctx.project_id.

    Hermes treats ``PHOTON_PROJECT_ID`` as canonical.  This check only proves
    the Dashboard project the operator sees maps back to that runtime id.
    """
    try:
        project = photon_auth.find_dashboard_project_for_spectrum_id(
            token,
            ctx.project_id,
        )
    except photon_auth.PhotonDashboardAuthError as e:
        raise _failed_invariant(
            ctx,
            step="dashboard project mapping",
            summary="Photon dashboard token could not verify the runtime project",
            expected="dashboard project list is readable for the current account",
            observed=f"{type(e).__name__}: {e}",
            evidence={"project_id": ctx.project_id},
            repair="log in to the Photon dashboard account that owns this project, then rerun quick-setup",
        ) from e
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="dashboard project mapping",
            summary="could not verify the dashboard project for the runtime project",
            expected="dashboard project list contains the current PHOTON_PROJECT_ID",
            observed=f"{type(e).__name__}: {e}",
            evidence={"project_id": ctx.project_id},
            repair="check Photon dashboard API access, then rerun quick-setup",
        ) from e

    if not project:
        raise _failed_invariant(
            ctx,
            step="dashboard project mapping",
            summary="stored Spectrum project is not visible in the Photon dashboard account",
            expected="one dashboard project maps to the current PHOTON_PROJECT_ID",
            observed={
                "project_id": ctx.project_id,
                "project_name": ctx.project_name,
            },
            evidence={"dashboard_host": _dashboard_url().rstrip("/")},
            repair="select a project visible in this dashboard account, or rerun quick-setup with --new-project",
        )

    ctx.dashboard_project_name = str(project.get("name") or ctx.project_name)
    print(
        "  ✓ dashboard project maps to runtime project "
        f"({ctx.dashboard_project_name})"
    )
    return project


def _ensure_active_home_available(ctx: _PhotonSetupContext) -> None:
    mismatch = photon_tunnel.active_home_mismatch()
    if not mismatch:
        print("[owner] Photon active-home claim is available for this Hermes home")
        return
    owner_home, current_home = mismatch
    raise _failed_invariant(
        ctx,
        step="active Hermes home ownership",
        summary="Photon project is claimed by another Hermes home",
        expected="active-home owner is empty or matches the current Hermes home",
        observed={
            "owner_home": owner_home,
            "current_home": current_home,
            "active_home_file": str(photon_tunnel.active_home_path()),
            "record": photon_tunnel.active_home_record(),
        },
        repair="run quick-setup from the owning Hermes home, or reset Photon from that home before trying this one",
    )


def _ensure_operator_phone(ctx: _PhotonSetupContext) -> None:
    print("[phone] Reconciling Spectrum shared iMessage user...")
    phone = ctx.args.phone or _prompt(
        f"Your iMessage phone number (E.164, format {_PHONE_FORMAT}): "
    )
    ctx.operator_phone = phone or None
    if not phone:
        raise _failed_invariant(
            ctx,
            step="operator phone",
            summary="operator phone number is required",
            expected=f"an E.164 phone number like {_PHONE_FORMAT}",
            observed="missing --phone and no interactive phone was provided",
            repair=f"rerun `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}`",
        )
    if not photon_auth.E164_RE.match(phone):
        raise _failed_invariant(
            ctx,
            step="operator phone",
            summary="operator phone number is not E.164",
            expected=f"format {_PHONE_FORMAT}",
            observed=phone,
            repair=f"rerun with a phone number like {_PHONE_ARG_PLACEHOLDER}",
        )

    user: dict[str, Any] = {}
    try:
        created_user = photon_auth.create_user(
            ctx.project_id,
            ctx.project_secret,
            phone_number=phone,
            first_name=ctx.args.first_name,
            last_name=ctx.args.last_name,
            email=ctx.args.email,
        )
    except Exception as e:
        if _error_looks_like_existing_user(e):
            print("  phone already exists; verifying it belongs to the current Photon project")
            user = _require_project_user_by_phone(ctx, phone, cause=e)
            print("  ✓ phone verified in the current Photon project")
        else:
            raise _failed_invariant(
                ctx,
                step="Spectrum user",
                summary="could not create or verify the Spectrum user",
                expected="operator phone exists as a shared Spectrum iMessage user",
                observed=_format_exception_with_http_detail(e),
                evidence={"project_id": ctx.project_id, "http_status": _http_status(e)},
                repair="verify the phone number in Photon, then rerun quick-setup",
            ) from e
    else:
        # Creation is scoped to PHOTON_PROJECT_ID, so a 2xx response proves the
        # user was created under the canonical project.  A follow-up lookup is
        # best-effort so we can print the assigned iMessage number when Photon
        # exposes it outside the create response.
        user = photon_auth.normalize_user(created_user)
        looked_up = _lookup_project_user_by_phone(ctx, phone)
        if looked_up:
            user = looked_up

    ctx.assigned_phone_number = _extract_assigned_phone_number(user)
    if ctx.assigned_phone_number:
        print(f"  ✓ assigned Photon iMessage number: {ctx.assigned_phone_number}")
    else:
        print(
            "  ✓ Spectrum user is verified in the current project; "
            "Photon did not return the assigned iMessage number"
        )
    if not _ensure_operator_phone_allowed(phone):
        raise _failed_invariant(
            ctx,
            step="sender access",
            summary="operator phone was not authorized in Hermes sender access",
            expected="operator phone is present in PHOTON_ALLOWED_USERS or access is open",
            observed=_photon_sender_access_status(),
            repair=f"run `hermes photon allow-phone {phone}`",
        )


def _lookup_project_user_by_phone(
    ctx: _PhotonSetupContext,
    phone: str,
) -> Optional[dict[str, Any]]:
    try:
        return photon_auth.find_project_user_by_phone(
            ctx.project_id,
            ctx.project_secret,
            phone,
        )
    except Exception:
        return None


def _require_project_user_by_phone(
    ctx: _PhotonSetupContext,
    phone: str,
    *,
    cause: BaseException,
) -> dict[str, Any]:
    try:
        user = photon_auth.find_project_user_by_phone(
            ctx.project_id,
            ctx.project_secret,
            phone,
        )
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="Spectrum user",
            summary="phone exists, but Hermes could not verify it in the current Photon project",
            expected="current PHOTON_PROJECT_ID user list contains the requested phone",
            observed=f"{type(e).__name__}: {e}",
            evidence={
                "project_id": ctx.project_id,
                "phone": phone,
                "create_user_error": _format_exception_with_http_detail(cause),
                "http_status": _http_status(cause),
            },
            repair="verify the phone under the Photon project that matches this project id, then rerun quick-setup",
        ) from e

    if user:
        return user
    raise _failed_invariant(
        ctx,
        step="Spectrum user",
        summary="phone exists, but not in the current Photon project",
        expected="current PHOTON_PROJECT_ID user list contains the requested phone",
        observed={
            "project_id": ctx.project_id,
            "phone": phone,
            "create_user_error": _format_exception_with_http_detail(cause),
            "http_status": _http_status(cause),
        },
        evidence={"dashboard_project": ctx.dashboard_project_name or ctx.project_name},
        repair="attach this phone to the selected Photon project or create a new Photon project for Hermes",
    )


def _ensure_sidecar_ready(ctx: _PhotonSetupContext) -> None:
    print("[sidecar] Verifying Node sidecar dependencies...")
    node_bin = os.getenv("PHOTON_NODE_BIN") or "node"
    if not shutil.which(node_bin):
        raise _failed_invariant(
            ctx,
            step="sidecar dependencies",
            summary="Node.js is not available for the Photon sidecar",
            expected="Node.js 20.18.1+ is on PATH or PHOTON_NODE_BIN points to it",
            observed=f"missing node binary: {node_bin}",
            evidence={"sidecar_dir": str(_SIDECAR_DIR)},
            repair="install Node.js 20.18.1+, then rerun quick-setup",
        )

    status = _sidecar_dependency_status()
    if status.startswith("✓"):
        print(f"  {status}")
        return
    if getattr(ctx.args, "skip_sidecar_install", False):
        raise _failed_invariant(
            ctx,
            step="sidecar dependencies",
            summary="Photon sidecar dependencies are not installed",
            expected="spectrum-ts dependency is installed and current",
            observed=status,
            evidence={"sidecar_dir": str(_SIDECAR_DIR)},
            repair="rerun quick-setup without `--skip-sidecar-install`",
        )

    rc = _install_sidecar()
    if rc != 0:
        raise _failed_invariant(
            ctx,
            step="sidecar dependencies",
            summary="npm install failed for the Photon sidecar",
            expected="npm install completes successfully",
            observed=f"npm exited with {rc}",
            evidence={"sidecar_dir": str(_SIDECAR_DIR)},
            repair="fix npm/Node errors shown above, then rerun quick-setup",
        )
    status = _sidecar_dependency_status()
    if not status.startswith("✓"):
        raise _failed_invariant(
            ctx,
            step="sidecar dependencies",
            summary="Photon sidecar dependencies still are not runnable after install",
            expected="spectrum-ts dependency is installed and current",
            observed=status,
            evidence={"sidecar_dir": str(_SIDECAR_DIR)},
            repair="inspect npm output in the sidecar directory, then rerun quick-setup",
        )
    print(f"  {status}")


def _ensure_sidecar_port_available(ctx: _PhotonSetupContext) -> None:
    port = _sidecar_port()
    owner = _sidecar_port_owner(port)
    if not owner.get("present"):
        print(f"  ✓ sidecar port available ({_DEFAULT_SIDECAR_BIND}:{port})")
        return

    current_home = _canonical_path_str(ctx.hermes_home)
    owner_home_raw = str(owner.get("hermes_home") or "").strip()
    owner_home = _canonical_path_str(owner_home_raw) if owner_home_raw else ""
    is_photon_sidecar = bool(owner.get("is_photon_sidecar"))
    is_other_home = bool(owner_home and owner_home != current_home)
    is_orphan = str(owner.get("ppid") or "").strip() == "1"

    if is_photon_sidecar and is_other_home and is_orphan:
        pid = str(owner.get("pid") or "").strip()
        if pid and _terminate_process(pid):
            print(
                f"  ✓ stopped stale Photon sidecar pid {pid} from {owner_home}"
            )
            return
        raise _sidecar_port_failure(
            ctx,
            owner,
            summary="stale Photon sidecar from another Hermes home could not be stopped",
            repair=(
                f"stop pid {pid or '<unknown>'} manually, or set "
                "PHOTON_SIDECAR_PORT to a free port, then rerun quick-setup"
            ),
        )

    if (
        is_photon_sidecar
        and owner_home == current_home
        and _inspect_gateway_runtime().get("running")
    ):
        print(
            f"  ✓ sidecar port already owned by current-home Photon sidecar "
            f"(pid {owner.get('pid')})"
        )
        return

    if is_photon_sidecar and is_other_home:
        summary = "Photon sidecar port is owned by another Hermes home"
    elif is_photon_sidecar:
        summary = "Photon sidecar port is already owned by a stale sidecar"
    else:
        summary = "Photon sidecar port is already in use"
    raise _sidecar_port_failure(
        ctx,
        owner,
        summary=summary,
        repair=(
            f"stop pid {owner.get('pid') or '<unknown>'}, or set "
            "PHOTON_SIDECAR_PORT to a free port, then rerun quick-setup"
        ),
    )


def _sidecar_port_failure(
    ctx: _PhotonSetupContext,
    owner: dict[str, Any],
    *,
    summary: str,
    repair: str,
) -> _FailedInvariant:
    port = int(owner.get("port") or _sidecar_port())
    raise _failed_invariant(
        ctx,
        step="sidecar port",
        summary=summary,
        expected=f"current Hermes home can bind {_DEFAULT_SIDECAR_BIND}:{port}",
        observed=owner,
        evidence={"sidecar_port": port, "sidecar_bind": _DEFAULT_SIDECAR_BIND},
        repair=repair,
    )


def _ensure_gateway_local_runtime(ctx: _PhotonSetupContext) -> None:
    print("[gateway] Ensuring current-home gateway serves local Photon health...")
    service = _service_for_current_home(
        ctx,
        _inspect_gateway_service_identity(ctx),
        announce=True,
    )
    runtime = _inspect_gateway_runtime()
    local = _check_local_health(ctx)
    ctx.local_health = local
    if local.ok and runtime.get("running"):
        print(f"  ✓ local health reachable ({local.url})")
        return
    if local.ok and not runtime.get("running"):
        raise _failed_invariant(
            ctx,
            step="gateway service identity",
            summary="local Photon health is served by a gateway outside this Hermes home",
            expected="local health is served by the current Hermes home gateway",
            observed={
                "local_health": _health_evidence(local),
                "current_home_gateway": runtime,
                "port_owner": _port_owner(ctx.webhook_port),
            },
            evidence={"service": service},
            repair="stop the other gateway or rerun quick-setup from the Hermes home that owns the port",
        )

    owner = _port_owner(ctx.webhook_port)
    if owner.get("present") and not runtime.get("running"):
        raise _failed_invariant(
            ctx,
            step="local webhook port",
            summary="Photon webhook port is owned by another process",
            expected=f"current-home gateway can bind 127.0.0.1:{ctx.webhook_port}",
            observed=owner,
            evidence={"local_health": _health_evidence(local), "service": service},
            repair=f"stop the process using port {ctx.webhook_port}, then rerun quick-setup",
        )

    _start_current_home_gateway(ctx, service)
    _wait_for_local_health(ctx, reason="gateway startup")


def _ensure_public_webhook_path(
    ctx: _PhotonSetupContext,
    *,
    wait_for_health: bool = True,
) -> None:
    print("[tunnel] Ensuring public webhook path...")
    configured = (_get_env_value("PHOTON_WEBHOOK_PUBLIC_URL") or "").strip()
    if configured and not photon_tunnel.is_trycloudflare_url(configured):
        ctx.webhook_url = configured
        print(f"  using user-owned public webhook URL: {configured}")
        if wait_for_health:
            _wait_for_public_health(ctx, reason="user-owned public URL")
        return

    result = photon_tunnel.start(on_install=print)
    if not result.success:
        raise _failed_invariant(
            ctx,
            step="managed tunnel",
            summary="Cloudflare Quick Tunnel did not start",
            expected="managed tunnel publishes a trycloudflare.com URL",
            observed=result.error or "unknown cloudflared failure",
            evidence={
                "cloudflared_log": str(result.log_path) if result.log_path else "",
                "command": result.command,
                "local_health": _health_evidence(ctx.local_health),
            },
            repair="inspect the cloudflared log and rerun quick-setup; install cloudflared manually if managed install failed",
        )
    ctx.webhook_url = result.webhook_url
    action = "reused" if result.reused else "started"
    print(f"  ✓ {action} managed tunnel: {result.webhook_url}")
    if wait_for_health:
        _wait_for_public_health(ctx, reason="managed tunnel startup")


def _ensure_current_webhook_registered(
    ctx: _PhotonSetupContext,
) -> _WebhookEnsureResult:
    print("[webhook] Reconciling Photon webhook registration...")
    try:
        hooks = photon_auth.list_webhooks(ctx.project_id, ctx.project_secret)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="registered webhook state",
            summary="could not list Photon webhooks",
            expected="Spectrum API returns registered webhooks for the current project",
            observed=f"{type(e).__name__}: {e}",
            evidence={"project_id": ctx.project_id, "http_status": _http_status(e)},
            repair="validate Photon project credentials, then rerun quick-setup",
        ) from e

    hooks = _delete_stale_managed_webhooks(
        ctx.project_id,
        ctx.project_secret,
        hooks,
        keep_url=ctx.webhook_url,
    )
    matching_hooks = [hook for hook in hooks if _webhook_url(hook) == ctx.webhook_url]
    if matching_hooks and _webhook_secret_present():
        public_changed = _save_public_webhook_url_checked(ctx, ctx.webhook_url)
        _claim_active_home_checked(ctx)
        verified = _verify_current_webhook_registered(ctx)
        print("  ✓ current webhook URL is registered and local signing secret is present")
        return _WebhookEnsureResult(
            hooks=verified,
            registered=True,
            public_url_changed=public_changed,
        )

    if matching_hooks:
        if photon_tunnel.is_trycloudflare_url(ctx.webhook_url):
            deleted = _delete_matching_webhook(
                ctx.project_id,
                ctx.project_secret,
                matching_hooks,
                ctx.webhook_url,
                reason="managed webhook with missing local signing secret",
            )
            if not deleted:
                raise _failed_invariant(
                    ctx,
                    step="webhook signing secret",
                    summary="current managed webhook exists but local signing secret is missing",
                    expected="local PHOTON_WEBHOOK_SECRET matches the registered current webhook",
                    observed={
                        "webhook_url": ctx.webhook_url,
                        "matching_webhook_ids": [_webhook_id(hook) for hook in matching_hooks],
                        "owned_webhook_ids": sorted(photon_tunnel.owned_webhook_ids()),
                    },
                    repair="delete the webhook manually only if it belongs to this setup, then rerun quick-setup",
                )
            hooks = [hook for hook in hooks if _webhook_url(hook) != ctx.webhook_url]
        else:
            raise _failed_invariant(
                ctx,
                step="webhook signing secret",
                summary="current user-owned webhook exists but local signing secret is missing",
                expected="PHOTON_WEBHOOK_SECRET is present for the registered webhook URL",
                observed={"webhook_url": ctx.webhook_url},
                repair="recreate the webhook in Photon and save the returned signing secret locally",
            )

    try:
        data = photon_auth.register_webhook(
            ctx.project_id,
            ctx.project_secret,
            webhook_url=ctx.webhook_url,
        )
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="webhook registration",
            summary="Photon rejected the current webhook URL registration",
            expected="Spectrum API registers the current public webhook URL",
            observed=f"{type(e).__name__}: {e}",
            evidence={
                "webhook_url": ctx.webhook_url,
                "project_id": ctx.project_id,
                "http_status": _http_status(e),
                "public_health": _public_health_evidence(ctx.public_health),
            },
            repair="fix the public webhook URL or Photon project credentials, then rerun quick-setup",
        ) from e

    webhook_id = _webhook_id(data)
    if webhook_id and photon_tunnel.is_trycloudflare_url(ctx.webhook_url):
        photon_tunnel.record_owned_webhook(webhook_id, ctx.webhook_url)
    if not photon_auth.persist_webhook_signing_secret(data, on_summary=print):
        raise _failed_invariant(
            ctx,
            step="webhook signing secret",
            summary="Photon did not return or Hermes could not save the webhook signing secret",
            expected="registration response includes a signing secret saved to Hermes env",
            observed={
                "webhook_id": webhook_id or "",
                "webhook_url": ctx.webhook_url,
                "env_path": str(ctx.env_path),
            },
            repair="inspect env file permissions; do not retry without first deleting the orphaned owned webhook if one was created",
        )
    public_changed = _save_public_webhook_url_checked(ctx, ctx.webhook_url)
    _claim_active_home_checked(ctx)
    verified = _verify_current_webhook_registered(ctx)
    print("  ✓ current webhook URL registered and signing secret saved")
    return _WebhookEnsureResult(
        hooks=verified,
        registered=True,
        secret_changed=True,
        public_url_changed=public_changed,
    )


def _record_webhook_runtime_changes(
    ctx: _PhotonSetupContext,
    result: _WebhookEnsureResult,
) -> None:
    ctx.registered_hooks = result.hooks
    ctx.runtime_secrets_changed = (
        ctx.runtime_secrets_changed
        or result.secret_changed
        or result.public_url_changed
    )


def _ensure_photon_gateway_platform_enabled(ctx: _PhotonSetupContext) -> None:
    """Persist the explicit gateway platform bit quick-setup relies on.

    The gateway has an env-driven plugin auto-enable path, but quick-setup's
    invariant is stronger: the gateway it starts for this Hermes home must
    load Photon.  Persisting ``platforms.photon.enabled`` makes that contract
    independent of inherited process env and silent auto-enable skips.
    """
    config_path = ctx.hermes_home / "config.yaml"
    try:
        from utils import atomic_roundtrip_yaml_update  # type: ignore

        atomic_roundtrip_yaml_update(config_path, "platforms.photon.enabled", True)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="gateway platform config",
            summary="could not enable Photon in gateway config",
            expected="config.yaml contains platforms.photon.enabled=true",
            observed=f"{type(e).__name__}: {e}",
            evidence={"config_path": str(config_path)},
            repair="fix config.yaml permissions or syntax, then rerun quick-setup",
        ) from e

    try:
        from gateway.config import load_gateway_config, Platform  # type: ignore
        from hermes_constants import (  # type: ignore
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(ctx.hermes_home)
        try:
            config = load_gateway_config()
        finally:
            reset_hermes_home_override(token)
        platform = Platform("photon")
        platform_cfg = config.platforms.get(platform)
        if platform_cfg and platform_cfg.enabled:
            return
        observed = {
            "platform_present": platform in config.platforms,
            "enabled": bool(platform_cfg.enabled) if platform_cfg else False,
            "configured_platforms": [p.value for p in config.platforms],
        }
    except Exception as e:
        observed = f"{type(e).__name__}: {e}"

    raise _failed_invariant(
        ctx,
        step="gateway platform config",
        summary="gateway config loader did not enable Photon",
        expected="load_gateway_config() returns platforms.photon.enabled=true",
        observed=observed,
        evidence={"config_path": str(config_path)},
        repair="inspect config.yaml and plugin discovery, then rerun quick-setup",
    )


def _restart_current_home_gateway(ctx: _PhotonSetupContext) -> None:
    print("[gateway] Restarting current-home gateway to load updated Photon secrets...")
    service = _service_for_current_home(
        ctx,
        _inspect_gateway_service_identity(ctx),
        announce=True,
    )
    try:
        if service.get("installed") and service.get("manager") == "launchd":
            from hermes_cli import gateway as gateway_cli  # type: ignore

            gateway_cli.launchd_restart()
            return
        if service.get("installed") and service.get("manager") == "systemd":
            from hermes_cli import gateway as gateway_cli  # type: ignore

            gateway_cli.systemd_restart(system=service.get("scope") == "system")
            return
        _launch_detached_gateway(ctx)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="gateway restart",
            summary="current-home gateway could not be restarted after Photon secrets changed",
            expected="gateway restarts and reloads Photon env values",
            observed=f"{type(e).__name__}: {e}",
            evidence={"service": service, "runtime": _inspect_gateway_runtime()},
            repair="repair the current-home gateway service, then rerun quick-setup",
        ) from e


def _restart_gateway_if_runtime_secrets_changed(
    ctx: _PhotonSetupContext,
    *,
    reason: str,
) -> None:
    if not ctx.runtime_secrets_changed:
        return
    runtime = _inspect_gateway_runtime()
    if not runtime.get("running"):
        return
    _restart_current_home_gateway(ctx)
    _wait_for_local_health(
        ctx,
        reason=f"gateway restart after {reason}",
    )
    ctx.runtime_secrets_changed = False


def _wait_for_photon_connected(ctx: _PhotonSetupContext, timeout_seconds: float = 60.0) -> None:
    print("[runtime] Waiting for gateway runtime status photon=connected...")
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        runtime = _inspect_gateway_runtime()
        last_status = runtime
        photon_state = (
            (runtime.get("status") or {})
            .get("platforms", {})
            .get("photon", {})
            .get("state")
        )
        if photon_state == "connected":
            print("  ✓ gateway runtime reports photon=connected")
            return
        if photon_state == "fatal":
            break
        _stream_quick_setup_logs(ctx)
        time.sleep(1)

    photon_status = (
        (last_status.get("status") or {})
        .get("platforms", {})
        .get("photon", {})
    )
    raise _failed_invariant(
        ctx,
        step="gateway runtime status",
        summary="gateway did not report photon=connected",
        expected="gateway_state includes platforms.photon.state=connected",
        observed=photon_status or last_status,
        evidence={
            "local_health": _health_evidence(ctx.local_health),
            "public_health": _public_health_evidence(ctx.public_health),
            "service": _inspect_gateway_service_identity(ctx),
            "runtime": last_status,
        },
        repair="inspect gateway logs for the Photon adapter error, then rerun quick-setup",
    )


def _start_current_home_gateway(ctx: _PhotonSetupContext, service: dict[str, Any]) -> None:
    service = _service_for_current_home(ctx, service, announce=True)
    try:
        if service.get("installed") and service.get("manager") == "launchd":
            from hermes_cli import gateway as gateway_cli  # type: ignore

            gateway_cli.launchd_start()
            return
        if service.get("installed") and service.get("manager") == "systemd":
            from hermes_cli import gateway as gateway_cli  # type: ignore

            gateway_cli.systemd_start(system=service.get("scope") == "system")
            return
        _launch_detached_gateway(ctx)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="gateway startup",
            summary="current-home gateway could not be started",
            expected="gateway process starts for the active Hermes home",
            observed=f"{type(e).__name__}: {e}",
            evidence={"service": service, "runtime": _inspect_gateway_runtime()},
            repair="repair the current-home gateway service or start `hermes gateway run --replace` from this Hermes home",
        ) from e


def _launch_detached_gateway(ctx: _PhotonSetupContext) -> None:
    log_dir = ctx.hermes_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "photon-quick-setup-gateway.log"
    env = _gateway_launch_env(ctx)
    env["HERMES_HOME"] = str(ctx.hermes_home)
    command = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
        "--replace",
    ]
    project_root = Path(__file__).resolve().parents[3]
    with log_path.open("ab") as log_file:
        subprocess.Popen(  # noqa: S603
            command,
            cwd=str(project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    ctx.gateway_started = True
    print(f"  started current-home gateway process (log: {log_path})")


def _gateway_launch_env(ctx: _PhotonSetupContext) -> dict[str, str]:
    """Build a child env with Photon values freshly read from this home."""
    env = os.environ.copy()
    file_values = _read_env_file_values(ctx.env_path)
    for key in _PHOTON_GATEWAY_ENV_KEYS:
        if key in file_values:
            env[key] = file_values[key]
            continue
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _read_env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore

        parsed = dotenv_values(path)
        return {
            str(key): str(value)
            for key, value in parsed.items()
            if key is not None and value is not None
        }
    except Exception:
        values: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
        except OSError:
            return {}
        return values


def _quick_setup_log_paths(ctx: _PhotonSetupContext) -> dict[str, Path]:
    log_dir = ctx.hermes_home / "logs"
    return {
        "gateway": log_dir / "gateway.log",
        "errors": log_dir / "errors.log",
        "gateway-error": log_dir / "gateway.error.log",
        "cloudflared": photon_tunnel.log_path(),
    }


def _print_quick_setup_log_paths(ctx: _PhotonSetupContext) -> None:
    print("[logs] Existing logs for this setup:")
    for label, path in _quick_setup_log_paths(ctx).items():
        print(f"  {label:<13}: {path}")
    if ctx.verbose:
        print("  verbose       : streaming new log lines while setup waits")


def _init_log_offsets(ctx: _PhotonSetupContext) -> None:
    ctx.log_offsets = {}
    for label, path in _quick_setup_log_paths(ctx).items():
        try:
            ctx.log_offsets[label] = path.stat().st_size
        except OSError:
            ctx.log_offsets[label] = 0


def _stream_quick_setup_logs(ctx: _PhotonSetupContext) -> None:
    if not ctx.verbose:
        return
    for label, path in _quick_setup_log_paths(ctx).items():
        offset = ctx.log_offsets.get(label, 0)
        try:
            with path.open("rb") as fh:
                fh.seek(max(0, offset))
                data = fh.read()
                ctx.log_offsets[label] = fh.tell()
        except OSError:
            continue
        if not data:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            rendered = _redact_log_line(line).strip()
            if rendered:
                print(f"[{label}] {rendered[:500]}")


def _collect_relevant_log_tail(
    ctx: _PhotonSetupContext,
    *,
    max_lines: int = 40,
) -> dict[str, list[str]]:
    logs: dict[str, list[str]] = {}
    for label, path in _quick_setup_log_paths(ctx).items():
        lines = _tail_text_file(path, max_lines=max_lines * 4)
        relevant = [
            _redact_log_line(line)
            for line in lines
            if _log_line_is_relevant(line)
        ]
        if not relevant:
            relevant = [_redact_log_line(line) for line in lines[-8:]]
        relevant = [line for line in relevant if line.strip()]
        if relevant:
            logs[label] = relevant[-max_lines:]
    return logs


def _tail_text_file(path: Path, *, max_lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-max_lines:]


def _log_line_is_relevant(line: str) -> bool:
    lowered = line.lower()
    markers = (
        "photon",
        "spectrum",
        "webhook",
        "cloudflared",
        "gateway",
        "health",
        "error",
        "failed",
        "fatal",
        "paused",
        "connected",
        "traceback",
        "exception",
        "unauthorized",
        "401",
    )
    return any(marker in lowered for marker in markers)


def _redact_log_line(line: str) -> str:
    redacted = str(line)
    redacted = re.sub(
        r"(?i)(authorization:\s*bearer\s+)[^\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(PHOTON_PROJECT_SECRET=)[^\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(PHOTON_WEBHOOK_SECRET=)[^\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(PHOTON_DASHBOARD_TOKEN=)[^\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r'(?i)("?(?:projectSecret|signingSecret|secret|token)"?\s*[:=]\s*")([^"]+)(")',
        r"\1<redacted>\3",
        redacted,
    )
    return redacted


def _finalize_failed_invariant_logs(
    error: _FailedInvariant,
    ctx: _PhotonSetupContext,
) -> None:
    _stream_quick_setup_logs(ctx)
    if not error.logs:
        error.logs = _collect_relevant_log_tail(ctx)


def _runtime_photon_status(runtime: dict[str, Any]) -> dict[str, Any]:
    status = runtime.get("status") or {}
    platforms = status.get("platforms") or {}
    photon = platforms.get("photon") or {}
    return photon if isinstance(photon, dict) else {}


def _runtime_photon_project_id(runtime: dict[str, Any]) -> str:
    photon = _runtime_photon_status(runtime)
    for key in ("project_id", "projectId", "spectrum_project_id", "spectrumProjectId"):
        value = photon.get(key)
        if value:
            return str(value)
    metadata = photon.get("metadata")
    if isinstance(metadata, dict):
        for key in ("project_id", "projectId", "spectrum_project_id", "spectrumProjectId"):
            value = metadata.get(key)
            if value:
                return str(value)
    for key in ("error_message", "message", "detail"):
        value = photon.get(key)
        if isinstance(value, str):
            parsed = _project_id_from_text(value)
            if parsed:
                return parsed
    return ""


def _project_id_from_text(text: str) -> str:
    match = re.search(
        r"/projects/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)",
        text or "",
    )
    return match.group(1) if match else ""


def _wait_for_local_health(
    ctx: _PhotonSetupContext,
    *,
    reason: str,
    timeout_seconds: float = 60.0,
) -> None:
    print(f"  waiting for local health: {_local_health_url(ctx)}")
    deadline = time.monotonic() + timeout_seconds
    last = _check_local_health(ctx)
    while time.monotonic() < deadline:
        if last.ok:
            ctx.local_health = last
            print(f"  ✓ local health reachable ({last.url})")
            return
        _stream_quick_setup_logs(ctx)
        time.sleep(1)
        last = _check_local_health(ctx)

    ctx.local_health = last
    raise _failed_invariant(
        ctx,
        step="local webhook health",
        summary="current-home gateway did not serve local Photon health",
        expected=f"{_local_health_url(ctx)} returns HTTP 200 with body ok",
        observed=_health_evidence(last),
        evidence={
            "reason": reason,
            "service": _inspect_gateway_service_identity(ctx),
            "runtime": _inspect_gateway_runtime(),
            "port_owner": _port_owner(ctx.webhook_port),
        },
        repair="inspect the gateway log and fix the Photon adapter startup error, then rerun quick-setup",
    )


def _wait_for_public_health(
    ctx: _PhotonSetupContext,
    *,
    reason: str,
    timeout_seconds: float = 60.0,
) -> None:
    print(f"  waiting for public health: {photon_tunnel.health_url_for_webhook_url(ctx.webhook_url)}")
    deadline = time.monotonic() + timeout_seconds
    last = photon_tunnel.check_public_health(ctx.webhook_url)
    while time.monotonic() < deadline:
        ctx.public_health = last
        if last[0]:
            print(f"  ✓ public health reachable ({last[1]})")
            return
        if not _public_health_can_be_transient(last[1]):
            break
        _stream_quick_setup_logs(ctx)
        time.sleep(2)
        last = photon_tunnel.check_public_health(ctx.webhook_url)

    ctx.public_health = last
    local = _check_local_health(ctx)
    ctx.local_health = local
    if not local.ok:
        raise _failed_invariant(
            ctx,
            step="public webhook health",
            summary="public webhook health failed because local Photon health is down",
            expected="local health works before Cloudflare forwards public health",
            observed={
                "public_health": _public_health_evidence(last),
                "local_health": _health_evidence(local),
            },
            evidence={
                "reason": reason,
                "service": _inspect_gateway_service_identity(ctx),
                "runtime": _inspect_gateway_runtime(),
                "port_owner": _port_owner(ctx.webhook_port),
            },
            repair="repair the current-home gateway local health before changing tunnel or webhook state",
        )
    if _public_health_is_system_dns_failure(last[1]):
        raise _failed_invariant(
            ctx,
            step="public webhook DNS",
            summary="this Mac cannot resolve the Cloudflare Quick Tunnel hostname",
            expected="system DNS resolves the public Quick Tunnel hostname",
            observed=_public_health_evidence(last),
            evidence={
                "reason": reason,
                "webhook_url": ctx.webhook_url,
                "local_health": _health_evidence(local),
                "managed_tunnel": photon_tunnel.status(),
            },
            repair=(
                "wait 30-60 seconds and rerun quick-setup; if it repeats: "
                "hermes photon webhook tunnel stop && "
                "hermes photon webhook tunnel start"
            ),
        )
    raise _failed_invariant(
        ctx,
        step="public webhook health",
        summary="public webhook URL did not forward to the local Photon gateway",
        expected="public /healthz returns HTTP 200 with body ok",
        observed=_public_health_evidence(last),
        evidence={
            "reason": reason,
            "webhook_url": ctx.webhook_url,
            "local_health": _health_evidence(local),
            "managed_tunnel": photon_tunnel.status(),
        },
        repair="restart the managed tunnel or repair the user-owned public URL, then rerun quick-setup",
    )


def _wait_for_public_health_with_managed_repair(
    ctx: _PhotonSetupContext,
    *,
    reason: str,
) -> None:
    try:
        _wait_for_public_health(ctx, reason=reason)
        return
    except _FailedInvariant as exc:
        if not _should_recycle_managed_tunnel(ctx, exc):
            raise
        print("  public managed tunnel health failed; recycling Quick Tunnel once")

    _recycle_managed_tunnel_and_retry_public_health(ctx, reason=reason)


def _should_recycle_managed_tunnel(
    ctx: _PhotonSetupContext,
    failure: _FailedInvariant,
) -> bool:
    if failure.step != "public webhook health":
        return False
    if not photon_tunnel.is_trycloudflare_url(ctx.webhook_url):
        return False
    local = ctx.local_health or _check_local_health(ctx)
    ctx.local_health = local
    return bool(local.ok)


def _recycle_managed_tunnel_and_retry_public_health(
    ctx: _PhotonSetupContext,
    *,
    reason: str,
) -> None:
    result = photon_tunnel.start(force_new=True, on_install=print)
    if not result.success:
        raise _failed_invariant(
            ctx,
            step="managed tunnel",
            summary="Cloudflare Quick Tunnel could not be recycled",
            expected="managed tunnel stops and publishes a fresh trycloudflare.com URL",
            observed=result.error or "unknown cloudflared failure",
            evidence={
                "reason": reason,
                "previous_webhook_url": ctx.webhook_url,
                "cloudflared_log": str(result.log_path) if result.log_path else "",
                "command": result.command,
                "local_health": _health_evidence(ctx.local_health),
            },
            repair="stop the managed tunnel manually, then rerun quick-setup",
        )

    previous_url = ctx.webhook_url
    ctx.webhook_url = result.webhook_url
    ctx.public_health = None
    print(f"  ✓ refreshed managed tunnel: {result.webhook_url}")

    webhook_result = _ensure_current_webhook_registered(ctx)
    _record_webhook_runtime_changes(ctx, webhook_result)
    _restart_gateway_if_runtime_secrets_changed(
        ctx,
        reason="managed tunnel URL changed",
    )
    _ensure_gateway_local_runtime(ctx)
    ctx.runtime_secrets_changed = False
    _wait_for_public_health(
        ctx,
        reason=f"{reason}; refreshed managed tunnel from {previous_url}",
    )


def _verify_current_webhook_registered(ctx: _PhotonSetupContext) -> list:
    try:
        hooks = photon_auth.list_webhooks(ctx.project_id, ctx.project_secret)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="registered webhook state",
            summary="could not verify registered webhooks after reconciliation",
            expected="Spectrum API lists the current webhook URL",
            observed=f"{type(e).__name__}: {e}",
            evidence={"project_id": ctx.project_id, "http_status": _http_status(e)},
            repair="check Photon Spectrum API access, then rerun quick-setup",
        ) from e
    if not any(_webhook_url(hook) == ctx.webhook_url for hook in hooks):
        raise _failed_invariant(
            ctx,
            step="registered webhook state",
            summary="current webhook URL is still not registered",
            expected="Photon registered webhook list contains the current webhook URL",
            observed={
                "webhook_url": ctx.webhook_url,
                "registered_webhooks": [
                    {"id": _webhook_id(hook), "url": _webhook_url(hook)}
                    for hook in hooks
                ],
            },
            repair="rerun quick-setup; if it repeats, inspect Photon dashboard webhook state",
        )
    return hooks


def _claim_active_home_checked(ctx: _PhotonSetupContext) -> None:
    try:
        photon_tunnel.record_active_hermes_home(
            project_id=ctx.project_id,
            webhook_url=ctx.webhook_url,
        )
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="active Hermes home ownership",
            summary="could not record the active Photon Hermes home",
            expected="active-home claim is written after webhook registration",
            observed=f"{type(e).__name__}: {e}",
            evidence={"active_home_file": str(photon_tunnel.active_home_path())},
            repair="fix permissions on the active-home file location, then rerun quick-setup",
        ) from e


def _save_public_webhook_url_checked(ctx: _PhotonSetupContext, url: str) -> bool:
    current = (_get_env_value("PHOTON_WEBHOOK_PUBLIC_URL") or "").strip()
    if current == url:
        return False
    try:
        from hermes_cli.config import save_env_value  # type: ignore

        save_env_value("PHOTON_WEBHOOK_PUBLIC_URL", url)
    except Exception as e:
        raise _failed_invariant(
            ctx,
            step="webhook env state",
            summary="could not save PHOTON_WEBHOOK_PUBLIC_URL",
            expected="current webhook URL is saved in Hermes env",
            observed=f"{type(e).__name__}: {e}",
            evidence={"env_path": str(ctx.env_path), "webhook_url": url},
            repair="fix Hermes env file permissions, then rerun quick-setup",
        ) from e
    return True


def _clear_local_project_runtime_state() -> None:
    for key in (
        "PHOTON_PROJECT_ID",
        "PHOTON_PROJECT_SECRET",
        "PHOTON_WEBHOOK_SECRET",
        "PHOTON_WEBHOOK_PUBLIC_URL",
    ):
        _remove_env_value(key)


def _remove_env_value(key: str) -> bool:
    try:
        from hermes_cli.config import remove_env_value  # type: ignore

        return bool(remove_env_value(key))
    except Exception:
        return os.environ.pop(key, None) is not None


def _error_looks_like_existing_user(exc: BaseException) -> bool:
    status = _http_status(exc)
    detail = _http_error_detail(exc).lower()
    if status == 409:
        return (
            ("already" in detail or "exist" in detail)
            and ("user" in detail or "phone" in detail)
        )
    fallback = str(exc).lower()
    return "already" in fallback and "user" in fallback


def _http_error_detail(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        return photon_auth._response_error_detail(response)  # type: ignore[attr-defined]
    except Exception:
        return str(getattr(response, "text", "") or "").strip()[:200]


def _format_exception_with_http_detail(exc: BaseException) -> str:
    formatted = f"{type(exc).__name__}: {exc}"
    detail = _http_error_detail(exc)
    if detail and detail not in formatted:
        return f"{formatted} ({detail})"
    return formatted


def _http_status(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _inspect_gateway_service_identity(ctx: _PhotonSetupContext) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "manager": "manual",
        "installed": False,
        "running": False,
        "path": "",
        "service_home": "",
        "expected_home": _canonical_path_str(ctx.hermes_home),
    }
    try:
        from hermes_cli import gateway as gateway_cli  # type: ignore
    except Exception as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
        return evidence

    try:
        if gateway_cli.is_macos():
            path = gateway_cli.get_launchd_plist_path()
            evidence.update({
                "manager": "launchd",
                "label": gateway_cli.get_launchd_label(),
                "path": str(path),
                "installed": path.exists(),
                "running": gateway_cli._probe_launchd_service_running(),
            })
            if path.exists():
                try:
                    data = plistlib.loads(path.read_bytes())
                except Exception as e:
                    evidence["parse_error"] = f"{type(e).__name__}: {e}"
                else:
                    env = data.get("EnvironmentVariables") or {}
                    evidence["service_home"] = str(env.get("HERMES_HOME") or "")
                    evidence["working_directory"] = str(data.get("WorkingDirectory") or "")
                    evidence["program"] = " ".join(
                        str(item) for item in (data.get("ProgramArguments") or [])
                    )
            return evidence

        if gateway_cli.supports_systemd_services():
            user_path = gateway_cli.get_systemd_unit_path(system=False)
            system_path = gateway_cli.get_systemd_unit_path(system=True)
            system_scope = system_path.exists() and not user_path.exists()
            path = system_path if system_scope else user_path
            evidence.update({
                "manager": "systemd",
                "scope": "system" if system_scope else "user",
                "name": gateway_cli.get_service_name(),
                "path": str(path),
                "installed": path.exists(),
            })
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                env = _parse_systemd_environment(text)
                evidence["service_home"] = env.get("HERMES_HOME", "")
                evidence["working_directory"] = _parse_systemd_value(text, "WorkingDirectory")
                evidence["program"] = _parse_systemd_value(text, "ExecStart")
                try:
                    result = gateway_cli._run_systemctl(
                        ["is-active", gateway_cli.get_service_name()],
                        system=system_scope,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    evidence["running"] = result.stdout.strip() == "active"
                except Exception as e:
                    evidence["running_error"] = f"{type(e).__name__}: {e}"
            return evidence

        if gateway_cli.is_windows():
            evidence["manager"] = "windows"
            try:
                from hermes_cli import gateway_windows  # type: ignore

                evidence["installed"] = gateway_windows.is_installed()
                evidence["running"] = bool(gateway_cli.find_gateway_pids())
            except Exception as e:
                evidence["error"] = f"{type(e).__name__}: {e}"
            return evidence
    except Exception as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
    return evidence


def _service_for_current_home(
    ctx: _PhotonSetupContext,
    service: dict[str, Any],
    *,
    announce: bool = False,
) -> dict[str, Any]:
    service_home = str(service.get("service_home") or "").strip()
    if not service_home:
        return service
    expected = _canonical_path_str(ctx.hermes_home)
    observed = _canonical_path_str(service_home)
    if observed == expected:
        return service

    ignored = dict(service)
    ignored.update({
        "ignored": True,
        "ignore_reason": (
            "installed gateway service HERMES_HOME does not match the current "
            "Hermes home"
        ),
        "ignored_service_home": observed,
        "current_home": expected,
        "installed": False,
        "running": False,
    })
    if announce and service.get("installed"):
        manager = str(service.get("manager") or "installed")
        print(
            f"  installed {manager} gateway service belongs to {observed}; "
            f"using a temporary gateway for {expected}"
        )
    return ignored


def _parse_systemd_environment(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("Environment="):
            continue
        body = line[len("Environment="):].strip()
        try:
            parts = shlex.split(body)
        except ValueError:
            parts = body.replace('"', "").split()
        for item in parts:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
    return parsed


def _parse_systemd_value(text: str, key: str) -> str:
    prefix = key + "="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _inspect_gateway_runtime() -> dict[str, Any]:
    try:
        from gateway import status as gateway_status  # type: ignore

        pid = gateway_status.get_running_pid(cleanup_stale=False)
        runtime = gateway_status.read_runtime_status() or {}
        return {
            "running": pid is not None,
            "pid": pid,
            "status_path": str(gateway_status._get_runtime_status_path()),
            "status": runtime,
        }
    except Exception as e:
        return {
            "running": False,
            "pid": None,
            "status_path": "",
            "status": {},
            "error": f"{type(e).__name__}: {e}",
        }


def _sidecar_port() -> int:
    raw = (_get_env_value("PHOTON_SIDECAR_PORT") or "").strip()
    if not raw:
        return _DEFAULT_SIDECAR_PORT
    try:
        port = int(raw)
    except ValueError:
        return _DEFAULT_SIDECAR_PORT
    return port if 1 <= port <= 65535 else _DEFAULT_SIDECAR_PORT


def _sidecar_port_owner(port: int) -> dict[str, Any]:
    owner = _port_owner(port)
    if not owner.get("present"):
        return owner
    pid = str(owner.get("pid") or "").strip()
    if not pid:
        return owner

    command = _process_command(pid)
    env = _process_env(pid)
    ppid = _process_parent_pid(pid)
    owner.update({
        "ppid": ppid,
        "full_command": command,
        "hermes_home": env.get("HERMES_HOME", ""),
        "photon_sidecar_port": env.get("PHOTON_SIDECAR_PORT", ""),
        "is_photon_sidecar": _is_photon_sidecar_process(command),
    })
    return owner


def _port_owner(port: int) -> dict[str, Any]:
    evidence: dict[str, Any] = {"present": False, "port": port}
    if os.name == "posix" and shutil.which("lsof"):
        try:
            proc = subprocess.run(  # noqa: S603
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            evidence["error"] = f"{type(e).__name__}: {e}"
        else:
            lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
            if len(lines) >= 2:
                parts = lines[1].split()
                evidence.update({
                    "present": True,
                    "command": parts[0] if len(parts) > 0 else "",
                    "pid": parts[1] if len(parts) > 1 else "",
                    "user": parts[2] if len(parts) > 2 else "",
                    "raw": lines[1],
                })
                return evidence

    # Fallback detects a listener even when owner metadata is unavailable.
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        evidence["present"] = sock.connect_ex(("127.0.0.1", port)) == 0
        if evidence["present"]:
            evidence["detail"] = "listener present; owner unavailable"
    except OSError as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
    finally:
        sock.close()
    return evidence


def _process_command(pid: str) -> str:
    if os.name != "posix":
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _process_parent_pid(pid: str) -> str:
    if os.name != "posix":
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _process_env(pid: str) -> dict[str, str]:
    if os.name != "posix":
        return {}
    try:
        proc = subprocess.run(  # noqa: S603
            ["ps", "eww", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    text = proc.stdout or ""
    env: dict[str, str] = {}
    for item in text.split():
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in {"HERMES_HOME", "PHOTON_SIDECAR_PORT", "PHOTON_WEBHOOK_PORT"}:
            env[key] = value
    return env


def _is_photon_sidecar_process(command: str) -> bool:
    normalized = command.replace("\\", "/")
    return "/plugins/platforms/photon/sidecar/index.mjs" in normalized


def _terminate_process(pid: str, timeout_seconds: float = 3.0) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid_int, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid_int):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid_int)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _check_local_health(ctx: _PhotonSetupContext, timeout_seconds: float = 2.0) -> _HealthResult:
    url = _local_health_url(ctx)
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            body = response.read(64).decode("utf-8", errors="replace").strip()
            status = int(getattr(response, "status", 0) or 0)
            if 200 <= status < 300 and body == "ok":
                return _HealthResult(True, url, "ok", status=status)
            return _HealthResult(False, url, f"HTTP {status}, body={body!r}", status=status)
    except urllib.error.HTTPError as e:
        return _HealthResult(False, url, f"HTTP Error {e.code}: {e.reason}", status=e.code)
    except Exception as e:
        return _HealthResult(False, url, f"{type(e).__name__}: {e}")


def _local_health_url(ctx: _PhotonSetupContext) -> str:
    return f"http://127.0.0.1:{ctx.webhook_port}/healthz"


def _health_evidence(health: Optional[_HealthResult]) -> dict[str, Any]:
    if health is None:
        return {"ok": False, "detail": "not checked"}
    return {
        "ok": health.ok,
        "url": health.url,
        "detail": health.detail,
        "status": health.status,
    }


def _public_health_evidence(
    health: Optional[tuple[bool, str]],
) -> dict[str, Any]:
    if health is None:
        return {"ok": False, "detail": "not checked"}
    return {"ok": bool(health[0]), "detail": health[1]}


def _canonical_path_str(value: Any) -> str:
    try:
        return str(Path(str(value)).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(Path(str(value)).expanduser().absolute())


def _failed_invariant(
    ctx: _PhotonSetupContext,
    *,
    step: str,
    summary: str,
    expected: str,
    observed: Any,
    repair: str,
    evidence: Optional[dict[str, Any]] = None,
) -> _FailedInvariant:
    merged: dict[str, Any] = {
        "hermes_home": str(ctx.hermes_home),
        "env_path": str(ctx.env_path),
        "webhook_port": ctx.webhook_port,
        "webhook_path": ctx.webhook_path,
    }
    if ctx.project_id:
        merged["project_id"] = ctx.project_id
    if ctx.webhook_url:
        merged["webhook_url"] = ctx.webhook_url
    if evidence:
        merged.update(evidence)
    return _FailedInvariant(
        step=step,
        summary=summary,
        expected=expected,
        observed=observed,
        evidence=merged,
        repair=repair,
    )


def _print_failed_invariant(error: _FailedInvariant) -> None:
    print("", file=sys.stderr)
    print(f"Photon quick setup stopped: {error.summary}", file=sys.stderr)
    print(f"  step     : {error.step}", file=sys.stderr)
    print(f"  expected : {error.expected}", file=sys.stderr)
    print("  observed :", file=sys.stderr)
    _print_evidence_value(error.observed, indent="    ", stream=sys.stderr)
    print("  evidence :", file=sys.stderr)
    _print_evidence_value(error.evidence, indent="    ", stream=sys.stderr)
    print(f"  repair   : {error.repair}", file=sys.stderr)
    if error.logs:
        print("  relevant logs :", file=sys.stderr)
        for label, lines in error.logs.items():
            print(f"    [{label}]", file=sys.stderr)
            for line in lines[-40:]:
                print(f"      {line}", file=sys.stderr)


def _print_evidence_value(value: Any, *, indent: str, stream: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list, tuple)):
                print(f"{indent}{key}:", file=stream)
                _print_evidence_value(item, indent=indent + "  ", stream=stream)
            else:
                print(f"{indent}{key}: {item}", file=stream)
        return
    if isinstance(value, (list, tuple)):
        if not value:
            print(f"{indent}-", file=stream)
            return
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                print(f"{indent}-", file=stream)
                _print_evidence_value(item, indent=indent + "  ", stream=stream)
            else:
                print(f"{indent}- {item}", file=stream)
        return
    print(f"{indent}{value}", file=stream)


def _print_quick_setup_reconciled(ctx: _PhotonSetupContext) -> None:
    print("Photon quick setup complete.")
    print(f"  Hermes home       : {ctx.hermes_home}")
    print(f"  env path          : {ctx.env_path}")
    print(f"  project id        : {ctx.project_id}")
    if ctx.dashboard_project_name:
        print(f"  dashboard         : visible as \"{ctx.dashboard_project_name}\"")
    print(
        "  local health      : "
        + (
            f"ok ({ctx.local_health.url})"
            if ctx.local_health and ctx.local_health.ok
            else "not verified"
        )
    )
    print(
        "  public health     : "
        + (
            f"ok ({ctx.public_health[1]})"
            if ctx.public_health and ctx.public_health[0]
            else "not verified"
        )
    )
    print(f"  webhook URL       : {ctx.webhook_url}")
    print("  gateway runtime   : photon=connected")
    _print_text_photon_number_step(ctx.outcome())


def interactive_setup() -> None:
    """Entry point used by `hermes setup gateway` when Photon is selected."""
    from hermes_cli.cli_output import print_info, prompt_yes_no

    project_id, project_secret = photon_auth.load_project_credentials()
    if (
        not photon_auth.load_photon_token()
        and not (project_id and project_secret)
    ):
        print_incomplete_setup_guidance()
        return

    if _interactive_setup_already_configured():
        print_info("Photon iMessage is already configured.")
        if not prompt_yes_no("Reconfigure Photon iMessage?", False):
            return

    args = argparse.Namespace(
        project_name=None,
        phone=None,
        first_name=None,
        last_name=None,
        email=None,
        no_browser=False,
        new_project=False,
        skip_sidecar_install=False,
    )
    rc = _cmd_quick_setup(args)
    if rc != 0:
        print_incomplete_setup_guidance()


def _interactive_setup_already_configured() -> bool:
    """Return True when this Hermes profile has enough Photon state to run."""
    if photon_tunnel.active_home_mismatch():
        return False
    project_id, project_secret = photon_auth.load_project_credentials()
    if not (project_id and project_secret):
        return False
    node_bin = os.getenv("PHOTON_NODE_BIN") or "node"
    if not shutil.which(node_bin):
        return False
    if not (_SIDECAR_DIR / "node_modules").exists():
        return False
    public_url = _get_env_value("PHOTON_WEBHOOK_PUBLIC_URL") or ""
    if not (public_url and _webhook_secret_present()):
        return False
    return _photon_sender_access_configured()


def print_login_first_guidance() -> None:
    """Explain the primary quick-setup entrypoint."""
    print()
    print("Run Photon quick setup:")
    print(f"        hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}")
    print("It will validate or run Photon login before reconciling runtime state.")


def print_incomplete_setup_guidance() -> None:
    """Print explicit next steps when Photon setup did not finish."""
    print()
    print("Photon iMessage setup is not complete yet.")
    project_id, project_secret = photon_auth.load_project_credentials()
    if (
        not photon_auth.load_photon_token()
        and not (project_id and project_secret)
    ):
        print("  Guided setup:")
        print(f"        hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}")
    else:
        print("  Guided setup:")
        print(f"        hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}")
    print("  Check exact status and next step:")
    print("        hermes photon status")
    print("  Docs:")
    print(f"        {_docs_paths()}")


def _setup_project_name(args: argparse.Namespace) -> str:
    return getattr(args, "project_name", None) or "Hermes Agent"


def _dashboard_url() -> str:
    return (
        os.getenv("PHOTON_DASHBOARD_HOST")
        or photon_auth.DEFAULT_DASHBOARD_HOST
    ).rstrip("/") + "/"


def _extract_assigned_phone_number(user: Any) -> Optional[str]:
    candidates: list[Any] = []
    for container in _candidate_user_payloads(user):
        candidates.extend([
            container.get("assignedPhoneNumber"),
            container.get("assigned_phone_number"),
            container.get("assignedNumber"),
            container.get("assigned_number"),
        ])
    for value in candidates:
        if isinstance(value, str) and photon_auth.E164_RE.match(value.strip()):
            return value.strip()
    return None


def _candidate_user_payloads(user: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(user, dict):
        payloads.append(user)
        for key in ("data", "user", "profile"):
            nested = user.get(key)
            if isinstance(nested, dict):
                payloads.append(nested)
    return payloads


def _print_quick_setup_complete(outcome: _SetupOutcome) -> None:
    print("Photon quick setup complete.")
    _print_text_photon_number_step(outcome)
    print("  Verify setup if needed:")
    print("        hermes photon status")
    print("  More details:")
    print(f"        {_docs_paths()}")


def _print_text_photon_number_step(outcome: _SetupOutcome) -> None:
    if outcome.assigned_phone_number:
        print("  Text Hermes:")
        print(f"        Send \"hi Hermes\" to {outcome.assigned_phone_number}")
        return

    phone_label = outcome.operator_phone or "your phone number"
    print("  Find the assigned Photon number:")
    print(f"        Open {_dashboard_url()}")
    print(f"        Open project \"{outcome.project_name}\"")
    print(f"        Users -> {phone_label} -> assigned iMessage number")
    print("        Send \"hi Hermes\" to that number")


def _cmd_allow_phone(args: argparse.Namespace) -> int:
    return 0 if _ensure_operator_phone_allowed(args.phone, explicit=True) else 1


def _ensure_operator_phone_allowed(phone: str, *, explicit: bool = False) -> bool:
    try:
        status = photon_auth.ensure_phone_allowed(phone)
    except Exception as e:
        print(f"allow-phone failed: {e}", file=sys.stderr)
        return False

    if status == "added":
        print("  ✓ phone authorized for Photon gateway access")
    elif status == "allow_all":
        print("  ✓ Photon gateway access is already open via PHOTON_ALLOW_ALL_USERS")
    else:
        print("  ✓ phone already authorized for Photon gateway access")

    if explicit:
        print("  Next: restart the gateway if it is already running:")
        print("        hermes gateway restart")
    return True


def _resolve_setup_project(
    args: argparse.Namespace,
    token: str,
    *,
    total_steps: int = 4,
) -> tuple[str, str]:
    name = args.project_name or "Hermes Agent"
    if getattr(args, "new_project", False):
        print(f"[2/{total_steps}] Creating new Photon project '{name}' (spectrum=true, imessage)...")
        return _create_and_store_project(token, name=name, source="explicit-new")

    existing_id, existing_secret = photon_auth.load_project_credentials()
    if existing_id and existing_secret:
        print(f"[2/{total_steps}] Reusing existing Photon project")
        return existing_id, existing_secret

    print(f"[2/{total_steps}] Looking for an existing Photon project...")
    try:
        projects = photon_auth.list_projects(token)
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return "", ""
    except Exception as e:
        print(
            "could not list Photon projects, so no new project was created. "
            f"Re-run with --new-project to create one explicitly. Details: {e}",
            file=sys.stderr,
        )
        return "", ""

    candidates = photon_auth.reusable_projects(projects, preferred_name=name)
    if len(candidates) == 1:
        candidate = _refresh_project_details(token, candidates[0])
        project_id = str(candidate.get("spectrum_project_id") or "")
        project_secret = str(candidate.get("project_secret") or "")
        if project_id and project_secret:
            _store_selected_project(candidate, source="remote-adopted")
            print("  ✓ adopted existing Photon project")
            return project_id, project_secret
        _print_project_choices(
            candidates,
            "Found an existing compatible Photon project, but Photon did not "
            "return a project secret for it.",
        )
        print(
            "No new project was created. Select a project whose credentials are "
            "available, or re-run with --new-project to create a replacement.",
            file=sys.stderr,
        )
        return "", ""

    if len(candidates) > 1:
        _print_project_choices(
            candidates,
            "Multiple compatible Photon projects were found.",
        )
        print(
            "No new project was created. Run `hermes photon projects select <id>` "
            "or re-run with --new-project.",
            file=sys.stderr,
        )
        return "", ""

    auto_created = bool(getattr(args, "auto_create_project", False))
    if auto_created:
        print("  No matching Photon project found; creating one for Hermes.")
    elif not _confirm_new_project(name):
        print(
            "No Photon project configured. Re-run with --new-project to create one.",
            file=sys.stderr,
        )
        return "", ""

    print(f"[2/{total_steps}] Creating Photon project '{name}' (spectrum=true, imessage)...")
    return _create_and_store_project(
        token,
        name=name,
        source="auto-new" if auto_created else "confirmed-new",
    )


def _create_and_store_project(token: str, *, name: str, source: str) -> tuple[str, str]:
    try:
        data = photon_auth.create_project(token, name=name)
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return "", ""
    except Exception as e:
        print(f"create-project failed: {e}", file=sys.stderr)
        return "", ""

    data = _complete_created_project_credentials(token, data)
    normalized = photon_auth.normalize_project(data)
    project_id = str(normalized.get("spectrum_project_id") or data.get("id") or "")
    project_secret = str(normalized.get("project_secret") or "")
    if not project_id or not project_secret:
        print(
            "create-project did not return spectrumProjectId + "
            "projectSecret. Re-run after enabling Spectrum on the "
            "project, or open https://app.photon.codes/ to fetch the "
            "secret manually.",
            file=sys.stderr,
        )
        return "", ""

    extra = {
        "name": name,
        "source": source,
        "created_by": "hermes-agent",
    }
    dashboard_project_id = normalized.get("dashboard_project_id")
    if dashboard_project_id and dashboard_project_id != project_id:
        extra["dashboard_project_id"] = dashboard_project_id
    platforms = normalized.get("platforms") or ["imessage"]
    extra["platforms"] = platforms
    photon_auth.store_project_credentials(project_id, project_secret, **extra)
    print("  ✓ project provisioned (run `hermes photon status` to see the id)")
    return project_id, project_secret


def _complete_created_project_credentials(token: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized = photon_auth.normalize_project(data)
    dashboard_project_id = str(
        normalized.get("dashboard_project_id") or data.get("id") or ""
    )
    if not dashboard_project_id:
        return data
    if normalized.get("spectrum_project_id") and normalized.get("project_secret"):
        return data

    try:
        details = photon_auth.get_project(token, dashboard_project_id)
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return data
    except Exception as e:
        print(
            "created Photon project, but could not fetch its Spectrum "
            f"credentials: {e}",
            file=sys.stderr,
        )
        details = {}
    if details:
        data = _merge_project_payloads(data, details)
        normalized = photon_auth.normalize_project(data)
        if normalized.get("spectrum_project_id") and normalized.get("project_secret"):
            print("  ✓ fetched Spectrum credentials for the new project")
            return data

    try:
        secret_data = photon_auth.regenerate_project_secret(
            token,
            dashboard_project_id,
        )
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return data
    except Exception as e:
        print(
            "created Photon project, but could not retrieve its Spectrum "
            f"secret: {e}",
            file=sys.stderr,
        )
        return data

    merged = _merge_project_payloads(data, secret_data)
    normalized = photon_auth.normalize_project(merged)
    if normalized.get("project_secret"):
        print("  ✓ retrieved Spectrum secret for the new project")
    return merged


def _refresh_project_details(token: str, project: dict[str, Any]) -> dict[str, Any]:
    normalized = photon_auth.normalize_project(project)
    dashboard_project_id = str(
        normalized.get("dashboard_project_id")
        or project.get("dashboard_project_id")
        or project.get("id")
        or ""
    )
    if not dashboard_project_id:
        return normalized
    if normalized.get("spectrum_project_id") and normalized.get("project_secret"):
        return normalized

    try:
        details = photon_auth.get_project(token, dashboard_project_id)
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return normalized
    except Exception as e:
        print(
            f"could not fetch Photon project details for {dashboard_project_id}: {e}",
            file=sys.stderr,
        )
        return normalized
    return photon_auth.normalize_project(
        _merge_project_payloads(project, details)
    )


def _merge_project_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if isinstance(payload, dict):
            merged.update(payload)
    return merged


def _store_selected_project(project: dict[str, Any], *, source: str) -> None:
    project_id = str(project.get("spectrum_project_id") or "")
    project_secret = str(project.get("project_secret") or "")
    extra = {
        "name": project.get("name") or "Photon Project",
        "platforms": project.get("platforms") or [],
        "source": source,
        "selected_at": int(time.time()),
        "created_by": "hermes-agent",
    }
    dashboard_project_id = project.get("dashboard_project_id")
    if dashboard_project_id and dashboard_project_id != project_id:
        extra["dashboard_project_id"] = dashboard_project_id
    photon_auth.store_project_credentials(project_id, project_secret, **extra)


def _confirm_new_project(name: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print()
    print("No existing Photon project was found for this Hermes setup.")
    print(f"Creating a new dashboard project named '{name}' may add another project to Photon.")
    answer = _prompt("Type CREATE NEW to continue: ")
    return answer == "CREATE NEW"


def _print_project_choices(projects: list[dict[str, Any]], heading: str) -> None:
    print()
    print(heading)
    for index, project in enumerate(projects, start=1):
        print(f"  {index}. {_project_summary(project)}")


def _project_summary(project: dict[str, Any]) -> str:
    name = project.get("name") or "(unnamed)"
    dashboard_id = project.get("dashboard_project_id") or "-"
    spectrum_id = project.get("spectrum_project_id") or "-"
    platforms = ",".join(project.get("platforms") or []) or "-"
    credentials = "yes" if project.get("project_secret") else "no"
    return (
        f"{name}  dashboard={dashboard_id}  spectrum={spectrum_id}  "
        f"platforms={platforms}  credentials={credentials}"
    )


def _cmd_status(_args: argparse.Namespace) -> int:
    ctx = _PhotonSetupContext.from_args(_args)
    # Defer the whole table to auth.print_credential_summary — its emit
    # callback is the only sink that sees credential-derived strings, so
    # cli.py keeps zero taint flow according to CodeQL.
    photon_auth.print_credential_summary(print)
    # The two non-credential rows live here so the helper stays purely
    # about credentials.
    node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node")
    sidecar_status = _sidecar_dependency_status()
    public_url = _get_env_value("PHOTON_WEBHOOK_PUBLIC_URL") or "✗ missing"
    tunnel_state = photon_tunnel.status()
    tunnel_label = _format_tunnel_status(tunnel_state)
    project_id, project_secret = photon_auth.load_project_credentials()
    registered_hooks: Optional[list] = None
    registered_error = ""
    spectrum_status = "✗ missing Photon project credentials"
    if project_id and project_secret:
        try:
            registered_hooks = photon_auth.list_webhooks(project_id, project_secret)
            spectrum_status = "✓ valid"
        except Exception as e:
            registered_error = str(e)
            status = _http_status(e)
            if status:
                spectrum_status = f"✗ invalid or unreachable (HTTP {status})"
            else:
                spectrum_status = f"✗ invalid or unreachable ({_short_error(str(e))})"
    else:
        registered_error = "missing Photon project credentials"
    service_identity = _inspect_gateway_service_identity(ctx)
    runtime_status = _inspect_gateway_runtime()
    local_health = _check_local_health(ctx)
    print(f"  Hermes home         : {photon_tunnel.hermes_home()}")
    print(f"  env path            : {ctx.env_path}")
    print(f"  dashboard auth      : {_dashboard_token_status()}")
    print(f"  Spectrum creds      : {spectrum_status}")
    print(f"  Photon owner        : {_active_home_status()}")
    print(f"  gateway service     : {_format_service_identity(service_identity)}")
    print(f"  gateway runtime     : {_format_runtime_status(runtime_status)}")
    print(f"  local health        : {_format_local_health_status(local_health)}")
    print(f"  node binary         : {node_bin or '✗ missing (install Node 20.18.1+)'}")
    print(f"  sidecar deps        : {sidecar_status}")
    print(f"  authorized phones   : {_photon_sender_access_status()}")
    print(f"  webhook public URL  : {public_url}")
    print(
        "  registered webhooks : "
        + _format_registered_webhook_status(
            registered_hooks,
            registered_error,
            public_url,
        )
    )
    print(f"  managed tunnel      : {tunnel_label}")
    public_health: Optional[tuple[bool, str]] = None
    if isinstance(public_url, str) and public_url.startswith("http"):
        healthy, detail = _check_public_health_for_status(public_url)
        public_health = (healthy, detail)
        health_label = f"✓ reachable ({detail})" if healthy else f"✗ unreachable ({detail})"
        print(f"  public health       : {health_label}")
    print(
        "  next step           : "
        + _next_status_step(
            sidecar_status,
            tunnel_state,
            registered_hooks=registered_hooks,
            registered_error=registered_error,
            public_health=public_health,
            local_health=local_health,
            service_identity=service_identity,
        )
    )
    print(f"  docs                : {_docs_paths()}")
    return 0


def _install_sidecar() -> int:
    npm = shutil.which("npm") or "npm"
    if not shutil.which(npm):
        print(
            "npm is not on PATH. Install Node.js 20.18.1+ (https://nodejs.org/) "
            "and re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"  $ cd {_SIDECAR_DIR} && {npm} install")
    proc = subprocess.run(  # noqa: S603
        [npm, "install"],
        cwd=str(_SIDECAR_DIR),
        check=False,
    )
    if proc.returncode != 0:
        print("npm install failed", file=sys.stderr)
    return proc.returncode


def _sidecar_dependency_status() -> str:
    if not (_SIDECAR_DIR / "node_modules").exists():
        return f"✗ run `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}`"

    version, problems = _installed_spectrum_ts()
    if not version:
        return (
            "✗ spectrum-ts missing; rerun "
            f"`hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}`"
        )

    parsed = _parse_semver(version)
    if parsed is None:
        return f"⚠ spectrum-ts {version} installed; unable to verify version"
    if parsed < _MIN_SPECTRUM_TS_VERSION:
        return (
            f"✗ spectrum-ts {version} is too old; "
            f"rerun `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}`"
        )
    if problems:
        detail = str(problems[0]).splitlines()[0][:120]
        return (
            f"⚠ spectrum-ts {version} installed but npm reports {detail}; "
            f"rerun `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}`"
        )
    return f"✓ installed (spectrum-ts {version})"


def _installed_spectrum_ts() -> tuple[Optional[str], list[Any]]:
    npm = shutil.which("npm") or "npm"
    if not shutil.which(npm):
        return None, ["npm missing"]
    try:
        proc = subprocess.run(  # noqa: S603
            [npm, "ls", "spectrum-ts", "--depth=0", "--json"],
            cwd=str(_SIDECAR_DIR),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, [str(e)]

    data: dict[str, Any] = {}
    if proc.stdout:
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
    deps = data.get("dependencies") or {}
    spectrum = deps.get("spectrum-ts") or {}
    version = spectrum.get("version")
    problems = data.get("problems") or []
    if proc.returncode != 0 and not problems:
        msg = (proc.stderr or "npm ls failed").strip()
        if msg:
            problems = [msg]
    return str(version) if version else None, list(problems)


def _parse_semver(version: str) -> Optional[tuple[int, int, int]]:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _cmd_projects(args: argparse.Namespace) -> int:
    token = photon_auth.load_photon_token()
    if not token:
        print("not logged in — run `hermes photon login` first", file=sys.stderr)
        return 1

    sub = getattr(args, "photon_projects_command", None)
    try:
        projects = photon_auth.list_projects(token)
    except photon_auth.PhotonDashboardAuthError as e:
        _handle_dashboard_auth_error(e)
        return 1
    except Exception as e:
        print(f"project list failed: {e}", file=sys.stderr)
        return 1
    normalized = [photon_auth.normalize_project(project) for project in projects]

    if sub == "list":
        if not normalized:
            print("No Photon projects found.")
            return 0
        print("Photon projects")
        for project in normalized:
            print("  " + _project_summary(project))
        return 0

    if sub == "select":
        requested = str(args.project_id)
        matches = [
            project for project in normalized
            if requested in {
                str(project.get("dashboard_project_id") or ""),
                str(project.get("spectrum_project_id") or ""),
            }
        ]
        if not matches:
            print(f"project not found: {requested}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            _print_project_choices(matches, "Multiple projects matched that id.")
            return 1
        project = _refresh_project_details(token, matches[0])
        if not (project.get("spectrum_enabled") and project.get("imessage_enabled")):
            print(
                "selected project is not a Spectrum iMessage project",
                file=sys.stderr,
            )
            return 1
        if not (project.get("spectrum_project_id") and project.get("project_secret")):
            print(
                "selected project cannot be adopted because Photon did not "
                "return spectrumProjectId + projectSecret for it",
                file=sys.stderr,
            )
            return 1
        _store_selected_project(project, source="manual-select")
        print("✓ selected Photon project")
        return 0

    print(f"unknown projects subcommand: {sub}", file=sys.stderr)
    return 2


def _handle_dashboard_auth_error(exc: photon_auth.PhotonDashboardAuthError) -> None:
    diagnostics = photon_auth.dashboard_auth_diagnostics()
    if diagnostics.get("token", {}).get("present"):
        _print_auth_diagnostics(diagnostics, stream=sys.stderr)
    photon_auth.clear_photon_token()
    print(str(exc), file=sys.stderr)
    print(
        "Cleared the saved Photon login token. Run `hermes photon login`, "
        "then retry the Photon command.",
        file=sys.stderr,
    )


def _print_auth_diagnostics(
    diagnostics: dict[str, Any],
    *,
    stream: Any = sys.stdout,
) -> None:
    token = diagnostics.get("token") or {}
    print("Photon auth diagnostics", file=stream)
    print("───────────────────────", file=stream)
    print(f"  env path        : {diagnostics.get('env_path')}", file=stream)
    print(f"  dashboard host  : {diagnostics.get('dashboard_host')}", file=stream)
    if diagnostics.get("candidate_source"):
        print(
            f"  token source    : {diagnostics.get('candidate_source')}",
            file=stream,
        )
    if token.get("present"):
        print(
            "  token           : present "
            f"(len={token.get('length')}, dots={token.get('dot_count')}, "
            f"jwt={_yes_no(bool(token.get('looks_jwt')))})",
            file=stream,
        )
    else:
        print("  token           : missing", file=stream)
    checks = diagnostics.get("checks") or []
    if checks:
        print("  endpoint checks :", file=stream)
        for check in checks:
            status = check.get("status")
            state = "ok" if check.get("ok") else "fail"
            detail = check.get("detail") or ""
            print(
                f"    - {check.get('name')} {check.get('path')} -> "
                f"{status} {state}; {detail}",
                file=stream,
            )


def _print_login_auth_debug(event: dict[str, Any]) -> None:
    kind = event.get("event")
    if kind == "device-token-response":
        token = event.get("token") or {}
        print("Photon login debug", file=sys.stderr)
        print("──────────────────", file=sys.stderr)
        print(
            f"  device token POST : {event.get('status')} "
            f"json={_yes_no(bool(event.get('body_is_json')))}",
            file=sys.stderr,
        )
        print(
            "  body keys         : "
            f"{_format_key_list(event.get('body_keys') or [])}",
            file=sys.stderr,
        )
        print(
            "  data keys         : "
            f"{_format_key_list(event.get('data_keys') or [])}",
            file=sys.stderr,
        )
        print(
            "  session keys      : "
            f"{_format_key_list(event.get('session_keys') or [])}",
            file=sys.stderr,
        )
        print(
            "  user object       : "
            f"{_yes_no(bool(event.get('user_present')))}",
            file=sys.stderr,
        )
        print(
            "  set-auth-token    : "
            f"{_yes_no(bool(event.get('has_set_auth_token_header')))}",
            file=sys.stderr,
        )
        print(
            "  selected token    : "
            f"{event.get('access_token_source')} "
            f"(len={token.get('length')}, dots={token.get('dot_count')}, "
            f"jwt={_yes_no(bool(token.get('looks_jwt')))})",
            file=sys.stderr,
        )
        candidates = event.get("candidates") or []
        if candidates:
            print(
                "  token candidates : "
                + _format_token_candidates(candidates),
                file=sys.stderr,
            )
        return

    if kind == "dashboard-validation":
        _print_auth_diagnostics(event, stream=sys.stderr)
        return

    print(f"Photon auth debug: {kind or 'unknown event'}", file=sys.stderr)


def _format_key_list(keys: list[Any]) -> str:
    return ", ".join(str(key) for key in keys) if keys else "-"


def _format_token_candidates(candidates: list[Any]) -> str:
    parts = []
    for candidate in candidates[:8]:
        if not isinstance(candidate, dict):
            continue
        shape = candidate.get("token") or {}
        parts.append(
            f"{candidate.get('source')}("
            f"len={shape.get('length')},"
            f"dots={shape.get('dot_count')},"
            f"jwt={_yes_no(bool(shape.get('looks_jwt')))})"
        )
    return ", ".join(parts) if parts else "-"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _cmd_webhook(args: argparse.Namespace) -> int:
    sub = getattr(args, "photon_webhook_command", None)
    if sub == "tunnel" and getattr(args, "photon_tunnel_command", None) != "start":
        return _cmd_webhook_tunnel(args)

    project_id, project_secret = photon_auth.load_project_credentials()
    if not (project_id and project_secret):
        print(
            f"no Photon project configured — run `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}` first",
            file=sys.stderr,
        )
        return 1

    if sub == "register":
        return _register_webhook_url(project_id, project_secret, args.url)

    if sub == "tunnel":
        return _cmd_webhook_tunnel(args)

    if sub == "list":
        try:
            data = photon_auth.list_webhooks(project_id, project_secret)
        except Exception as e:
            print(f"list failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2))
        return 0

    if sub == "delete":
        try:
            photon_auth.delete_webhook(
                project_id, project_secret, webhook_id=args.webhook_id
            )
        except Exception as e:
            print(f"delete failed: {e}", file=sys.stderr)
            return 1
        print(f"deleted webhook {args.webhook_id}")
        return 0

    print(f"unknown webhook subcommand: {sub}", file=sys.stderr)
    return 2


def _cmd_webhook_tunnel(args: argparse.Namespace) -> int:
    sub = getattr(args, "photon_tunnel_command", None)
    if sub == "start":
        return _start_managed_tunnel_and_register()

    if sub == "status":
        state = photon_tunnel.status()
        print("Photon managed webhook tunnel")
        print("─────────────────────────────")
        print(f"  status              : {_format_tunnel_status(state)}")
        print(f"  public URL          : {state.get('public_url') or '✗ missing'}")
        print(f"  webhook URL         : {state.get('webhook_url') or '✗ missing'}")
        print(f"  state file          : {state.get('state_path')}")
        print(f"  log file            : {state.get('log_path')}")
        print("  Next: " + (
            "hermes photon webhook tunnel start"
            if not state.get("running")
            else "hermes photon status"
        ))
        return 0

    if sub == "stop":
        result = photon_tunnel.stop()
        print(result.get("message") or "managed tunnel stopped")
        print("  Next: hermes photon webhook tunnel start")
        return 0

    if sub == "logs":
        logs = photon_tunnel.tail_logs()
        if not logs:
            print(f"No cloudflared logs yet ({photon_tunnel.log_path()})")
            return 0
        print(logs)
        return 0

    print(f"unknown webhook tunnel subcommand: {sub}", file=sys.stderr)
    return 2


def _start_managed_tunnel_and_register() -> int:
    result = photon_tunnel.start(on_install=print)
    if not result.success:
        print(f"cloudflared tunnel failed: {result.error}", file=sys.stderr)
        _print_cloudflared_install_help()
        if result.log_path:
            print(f"  Logs: {result.log_path}", file=sys.stderr)
        return 1

    action = "Reusing" if result.reused else "Started"
    print(f"  ✓ {action.lower()} Cloudflare Quick Tunnel")
    print(f"  public URL: {result.public_url}")
    print(f"  webhook URL: {result.webhook_url}")

    project_id, project_secret = photon_auth.load_project_credentials()
    if not (project_id and project_secret):
        print(
            f"no Photon project configured — run `hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}` first",
            file=sys.stderr,
        )
        return 1

    try:
        existing_hooks = photon_auth.list_webhooks(project_id, project_secret)
    except Exception as e:
        print(
            "could not check existing Photon webhooks, so no webhook was registered. "
            f"Details: {e}",
            file=sys.stderr,
        )
        return 1

    existing_hooks = _delete_stale_managed_webhooks(
        project_id,
        project_secret,
        existing_hooks,
        keep_url=result.webhook_url,
    )

    return _register_webhook_url(
        project_id,
        project_secret,
        result.webhook_url,
        existing_hooks=existing_hooks,
        recreate_managed_without_secret=True,
    )


def _register_webhook_url(
    project_id: str,
    project_secret: str,
    url: str,
    *,
    existing_hooks: Optional[list] = None,
    recreate_managed_without_secret: bool = False,
) -> int:
    if existing_hooks is None:
        try:
            existing_hooks = photon_auth.list_webhooks(project_id, project_secret)
        except Exception as e:
            print(
                "could not check existing Photon webhooks, so no new webhook "
                f"was registered. Details: {e}",
                file=sys.stderr,
            )
            return 1

    matching_hooks = [
        hook for hook in existing_hooks
        if _webhook_url(hook) == url
    ]
    if matching_hooks:
        if _webhook_secret_present():
            if not _save_public_webhook_url(url):
                return 1
            if not _claim_active_photon_home(project_id, url):
                return 1
            print("✓ webhook URL already registered; keeping existing local signing secret")
            print("  Next: restart the gateway if it was already running:")
            print("        hermes gateway restart")
            return 0
        if recreate_managed_without_secret and photon_tunnel.is_trycloudflare_url(url):
            deleted = _delete_matching_webhook(
                project_id,
                project_secret,
                matching_hooks,
                url,
                reason="managed webhook with missing local signing secret",
            )
            if not deleted:
                print(
                    "webhook URL is already registered, but it is not owned by "
                    "this Hermes profile. Refusing to delete it automatically.",
                    file=sys.stderr,
                )
                return 1
        else:
            print(
                "webhook URL is already registered, but PHOTON_WEBHOOK_SECRET "
                "is not set locally. Photon only returns the signing secret at "
                "registration time. Delete or recreate the webhook in the "
                "Photon dashboard, then save the new signing secret locally.",
                file=sys.stderr,
            )
            return 1

    try:
        data = photon_auth.register_webhook(
            project_id, project_secret, webhook_url=url
        )
    except Exception as e:
        print(f"register failed: {e}", file=sys.stderr)
        return 1
    webhook_id = _webhook_id(data)
    if webhook_id and photon_tunnel.is_trycloudflare_url(url):
        photon_tunnel.record_owned_webhook(webhook_id, url)
    # The helper does all the formatting + writing; cli.py never
    # touches the signing-secret value, the path it was written
    # to, or even the redacted-response dict. on_summary is a
    # plain printer callback.
    ok = photon_auth.persist_webhook_signing_secret(data, on_summary=print)
    if not ok:
        print(
            "‼  Photon returned no signing secret in the response, "
            "or the file write failed. Inspect your home directory "
            "permissions and re-run; do not retry without first "
            "deleting the orphaned webhook from the Photon dashboard.",
            file=sys.stderr,
        )
        return 1
    if not _save_public_webhook_url(url):
        return 1
    if not _claim_active_photon_home(project_id, url):
        return 1
    print("  ✓ webhook public URL saved")
    print("  Next: restart the gateway if it was already running:")
    print("        hermes gateway restart")
    return 0


def _claim_active_photon_home(project_id: str, webhook_url: str) -> bool:
    try:
        path = photon_tunnel.record_active_hermes_home(
            project_id=project_id,
            webhook_url=webhook_url,
        )
    except Exception as e:
        print(f"could not record active Photon Hermes home: {e}", file=sys.stderr)
        return False
    print(f"  ✓ Photon owner recorded at {path}")
    return True


def _delete_matching_webhook(
    project_id: str,
    project_secret: str,
    hooks: list,
    url: str,
    *,
    reason: str,
) -> int:
    owned_ids = photon_tunnel.owned_webhook_ids()
    deleted = 0
    for hook in hooks:
        if _webhook_url(hook) != url:
            continue
        webhook_id = _webhook_id(hook)
        if not webhook_id:
            continue
        if webhook_id not in owned_ids:
            print(f"refusing to delete unowned {reason}: {webhook_id}", file=sys.stderr)
            continue
        try:
            photon_auth.delete_webhook(
                project_id, project_secret, webhook_id=webhook_id
            )
        except Exception as e:
            print(f"could not delete {reason} {webhook_id}: {e}", file=sys.stderr)
            continue
        photon_tunnel.forget_owned_webhook(webhook_id)
        deleted += 1
        print(f"  ✓ deleted {reason}: {webhook_id}")
    return deleted


def _delete_stale_managed_webhooks(
    project_id: str,
    project_secret: str,
    hooks: list,
    *,
    keep_url: str,
) -> list:
    """Delete old managed Quick Tunnel webhooks for this Photon project.

    Cloudflare Quick Tunnel URLs are ephemeral. Keeping multiple
    trycloudflare.com webhooks on one Photon project makes setup appear
    healthy while Photon may deliver to an old profile/tunnel instead of
    the gateway the user just started.
    """
    deleted_ids: set[str] = set()
    deleted_urls: set[str] = set()
    owned_ids = photon_tunnel.owned_webhook_ids()
    for hook in hooks:
        url = _webhook_url(hook)
        webhook_id = _webhook_id(hook)
        if (
            not url
            or url == keep_url
            or not webhook_id
            or webhook_id not in owned_ids
            or not photon_tunnel.is_trycloudflare_url(url)
        ):
            continue
        try:
            photon_auth.delete_webhook(
                project_id,
                project_secret,
                webhook_id=webhook_id,
            )
        except Exception as e:
            print(
                f"could not delete stale managed trycloudflare.com webhook "
                f"{webhook_id}: {e}",
                file=sys.stderr,
            )
            continue
        deleted_ids.add(webhook_id)
        deleted_urls.add(url)
        photon_tunnel.forget_owned_webhook(webhook_id)
        print(f"  ✓ deleted stale managed trycloudflare.com webhook: {webhook_id}")

    if not deleted_ids and not deleted_urls:
        return hooks
    return [
        hook for hook in hooks
        if _webhook_id(hook) not in deleted_ids
        and _webhook_url(hook) not in deleted_urls
    ]


def _webhook_id(webhook: Any) -> str:
    if not isinstance(webhook, dict):
        return ""
    for key in ("id", "webhookId", "webhook_id", "uuid"):
        value = webhook.get(key)
        if value:
            return str(value)
    return ""


def _webhook_url(webhook: Any) -> str:
    if not isinstance(webhook, dict):
        return ""
    return str(webhook.get("webhookUrl") or webhook.get("url") or "")


def _get_env_value(key: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value  # type: ignore
        return get_env_value(key)
    except Exception:
        return os.getenv(key)


def _truthy_env(key: str) -> bool:
    return (_get_env_value(key) or "").strip().lower() in {"true", "1", "yes"}


def _photon_sender_access_configured() -> bool:
    if _truthy_env("PHOTON_ALLOW_ALL_USERS") or _truthy_env("GATEWAY_ALLOW_ALL_USERS"):
        return True
    allowed = photon_auth.load_allowed_phone_numbers()
    return bool(allowed)


def _photon_sender_access_status() -> str:
    if _truthy_env("PHOTON_ALLOW_ALL_USERS"):
        return "open (PHOTON_ALLOW_ALL_USERS=true)"
    if _truthy_env("GATEWAY_ALLOW_ALL_USERS"):
        return "open (GATEWAY_ALLOW_ALL_USERS=true)"
    allowed = photon_auth.load_allowed_phone_numbers()
    if "*" in allowed:
        return "open (PHOTON_ALLOWED_USERS=*)"
    if allowed:
        return f"{len(allowed)} configured"
    return "✗ none (unknown senders will request pairing)"


def _save_public_webhook_url(url: str) -> bool:
    try:
        from hermes_cli.config import save_env_value  # type: ignore
        save_env_value("PHOTON_WEBHOOK_PUBLIC_URL", url)
        return True
    except Exception as e:
        print(f"could not save PHOTON_WEBHOOK_PUBLIC_URL: {e}", file=sys.stderr)
        return False


def _webhook_secret_present() -> bool:
    return bool(_get_env_value("PHOTON_WEBHOOK_SECRET"))


def _format_registered_webhook_status(
    hooks: Optional[list],
    error: str,
    public_url: str,
) -> str:
    if hooks is None:
        detail = _short_error(error) if error else "unknown"
        return f"⚠ unavailable ({detail})"
    if not hooks:
        return "✗ none registered"

    current_url = public_url if public_url.startswith("http") else ""
    current_registered = bool(
        current_url and any(_webhook_url(hook) == current_url for hook in hooks)
    )
    owned_stale = len(_stale_managed_webhooks(hooks, keep_url=current_url, owned=True))
    unowned_stale = len(_stale_managed_webhooks(hooks, keep_url=current_url, owned=False))
    stale_detail = _format_stale_managed_detail(
        owned_stale=owned_stale,
        unowned_stale=unowned_stale,
    )
    count = len(hooks)
    if current_url and current_registered:
        if stale_detail:
            return f"⚠ {count} registered; current URL registered; {stale_detail}"
        return f"✓ {count} registered; current URL registered"
    if current_url:
        if stale_detail:
            return f"✗ {count} registered; current URL is not registered; {stale_detail}"
        return f"✗ {count} registered; current URL is not registered"
    if stale_detail:
        return f"⚠ {count} registered; {stale_detail}"
    return f"✓ {count} registered"


def _format_stale_managed_detail(*, owned_stale: int, unowned_stale: int) -> str:
    parts = []
    if owned_stale:
        parts.append(f"{owned_stale} owned stale managed")
    if unowned_stale:
        parts.append(f"{unowned_stale} unowned stale managed")
    return "; ".join(parts)


def _stale_managed_webhooks(
    hooks: list,
    *,
    keep_url: str,
    owned: Optional[bool] = None,
) -> list:
    owned_ids = photon_tunnel.owned_webhook_ids()
    stale_hooks = []
    for hook in hooks:
        url = _webhook_url(hook)
        if (
            not url
            or url == keep_url
            or not photon_tunnel.is_trycloudflare_url(url)
        ):
            continue
        is_owned = _webhook_id(hook) in owned_ids
        if owned is True and not is_owned:
            continue
        if owned is False and is_owned:
            continue
        stale_hooks.append(hook)
    return stale_hooks


def _short_error(error: str) -> str:
    return (error or "").replace("\n", " ")[:120] or "unknown"


def _format_tunnel_status(state: dict[str, Any]) -> str:
    if state.get("running"):
        return f"✓ running (pid {state.get('pid')})"
    if state.get("public_url") or state.get("webhook_url"):
        return "✗ stopped (run `hermes photon webhook tunnel start`)"
    return "✗ not started"


def _dashboard_token_status() -> str:
    token = photon_auth.load_photon_token()
    if not token:
        return "✗ missing"
    try:
        photon_auth.validate_photon_token(token)
    except Exception as e:
        return f"✗ invalid ({_short_error(str(e))})"
    return "✓ valid"


def _format_service_identity(service: dict[str, Any]) -> str:
    manager = service.get("manager") or "manual"
    if not service.get("installed"):
        return f"not installed ({manager})"
    parts = [f"{manager} installed"]
    if service.get("running"):
        parts.append("running")
    else:
        parts.append("stopped")
    service_home = str(service.get("service_home") or "").strip()
    expected = str(service.get("expected_home") or "").strip()
    if service_home:
        if expected and _canonical_path_str(service_home) != _canonical_path_str(expected):
            parts.append(f"wrong home: {service_home}")
        else:
            parts.append(f"home={service_home}")
    if service.get("path"):
        parts.append(f"path={service.get('path')}")
    return "; ".join(parts)


def _format_runtime_status(runtime: dict[str, Any]) -> str:
    if not runtime.get("running"):
        return "✗ not running for this Hermes home"
    status = runtime.get("status") or {}
    photon_state = (
        status.get("platforms", {})
        .get("photon", {})
        .get("state")
    )
    runtime_project_id = _runtime_photon_project_id(runtime)
    project_detail = f"; project={runtime_project_id}" if runtime_project_id else ""
    if photon_state:
        return f"pid {runtime.get('pid')}; photon={photon_state}{project_detail}"
    return f"pid {runtime.get('pid')}; photon=unknown{project_detail}"


def _format_local_health_status(health: _HealthResult) -> str:
    if health.ok:
        return f"✓ reachable ({health.url})"
    return f"✗ unreachable ({health.detail})"


def _active_home_status() -> str:
    record = photon_tunnel.active_home_record()
    owner = str(record.get("hermes_home") or "").strip()
    if not owner:
        return "not claimed"
    mismatch = photon_tunnel.active_home_mismatch()
    if not mismatch:
        return "this Hermes home"
    owner_home, current_home = mismatch
    return f"{owner_home} (current: {current_home})"


def _next_status_step(
    sidecar_status: str,
    tunnel_state: dict[str, Any],
    *,
    registered_hooks: Optional[list] = None,
    registered_error: str = "",
    public_health: Optional[tuple[bool, str]] = None,
    local_health: Optional[_HealthResult] = None,
    service_identity: Optional[dict[str, Any]] = None,
) -> str:
    if photon_tunnel.active_home_mismatch():
        return (
            "run `hermes photon quick-setup --phone "
            f"{_PHONE_ARG_PLACEHOLDER}` from the intended Hermes home"
        )
    project_id, project_secret = photon_auth.load_project_credentials()
    if not photon_auth.load_photon_token() and not (project_id and project_secret):
        return "hermes photon login"
    if not (project_id and project_secret):
        return f"hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}"
    if sidecar_status.startswith("✗"):
        return f"hermes photon quick-setup --phone {_PHONE_ARG_PLACEHOLDER}"
    public_url = _get_env_value("PHOTON_WEBHOOK_PUBLIC_URL") or ""
    if not (_webhook_secret_present() and public_url):
        return "hermes photon webhook tunnel start"
    if photon_tunnel.is_trycloudflare_url(public_url) and not tunnel_state.get("running"):
        return "hermes photon webhook tunnel start"
    if registered_hooks is not None:
        current_registered = any(
            _webhook_url(hook) == public_url for hook in registered_hooks
        )
        if not current_registered:
            return "hermes photon webhook tunnel start"
        public_health_step = _public_health_next_step(
            public_health,
            tunnel_state,
            public_url,
            local_health=local_health,
            service_identity=service_identity,
        )
        if public_health_step:
            return public_health_step
        if _stale_managed_webhooks(registered_hooks, keep_url=public_url, owned=True):
            return "hermes photon webhook tunnel start  (cleans owned stale managed webhooks)"
    elif registered_error:
        pass
    public_health_step = _public_health_next_step(
        public_health,
        tunnel_state,
        public_url,
        local_health=local_health,
        service_identity=service_identity,
    )
    if public_health_step:
        return public_health_step
    if not _photon_sender_access_configured():
        return f"hermes photon allow-phone {_PHONE_ARG_PLACEHOLDER}"
    try:
        from gateway.status import is_gateway_running, read_runtime_status  # type: ignore

        if is_gateway_running():
            runtime = read_runtime_status() or {}
            photon_state = (
                (runtime.get("platforms") or {})
                .get("photon", {})
                .get("state")
            )
            if photon_state == "connected":
                return "gateway is running; send an iMessage to the Photon number"
            return "gateway is running but Photon is not connected; run `hermes gateway restart`"
    except Exception:
        pass
    return "hermes gateway run -v  (or `hermes gateway restart` if already running)"


def _public_health_next_step(
    public_health: Optional[tuple[bool, str]],
    tunnel_state: dict[str, Any],
    public_url: str,
    *,
    local_health: Optional[_HealthResult] = None,
    service_identity: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    if not public_health:
        return None
    healthy, detail = public_health
    if healthy:
        return None

    detail_lower = (detail or "").lower()
    managed_quick_tunnel = photon_tunnel.is_trycloudflare_url(public_url)
    if managed_quick_tunnel and (
        "system dns failed" in detail_lower
        or "nodename nor servname" in detail_lower
        or "name or service not known" in detail_lower
        or "no address associated" in detail_lower
        or "http error 530" in detail_lower
    ):
        return (
            "wait 30-60s and rerun; if it repeats: "
            "hermes photon webhook tunnel stop && hermes photon webhook tunnel start"
        )

    if not tunnel_state.get("running"):
        if managed_quick_tunnel:
            return "hermes photon webhook tunnel start"
        return "repair the public webhook URL, then run `hermes photon webhook register ...`"

    if (
        "http error 502" in detail_lower
        or "bad gateway" in detail_lower
        or "connection refused" in detail_lower
        or "connection reset" in detail_lower
        or "timed out" in detail_lower
    ):
        if local_health is not None and not local_health.ok:
            if service_identity and "wrong home" in _format_service_identity(service_identity):
                return "repair the gateway service HERMES_HOME for this profile"
            return "start or repair the current-home gateway; local health is failing"
        return "hermes gateway restart  (then re-run `hermes photon status`)"

    return "hermes photon status  (retry; if still unreachable, run `hermes gateway restart`)"


def _check_public_health_for_status(public_url: str) -> tuple[bool, str]:
    healthy, detail = photon_tunnel.check_public_health(public_url)
    if healthy or not _public_health_can_be_transient(detail):
        return healthy, detail

    for _attempt in range(2):
        time.sleep(2)
        healthy, detail = photon_tunnel.check_public_health(public_url)
        if healthy or not _public_health_can_be_transient(detail):
            return healthy, detail
    return healthy, detail


def _public_health_can_be_transient(detail: str) -> bool:
    detail_lower = (detail or "").lower()
    return (
        "http error 502" in detail_lower
        or "bad gateway" in detail_lower
        or _public_health_is_system_dns_failure(detail)
    )


def _public_health_is_system_dns_failure(detail: str) -> bool:
    return "system dns failed" in (detail or "").lower()


def _docs_paths() -> str:
    return "plugins/platforms/photon/README.md; website/docs/user-guide/messaging/photon.md"


def _print_cloudflared_install_help() -> None:
    print("Install cloudflared manually, then re-run:", file=sys.stderr)
    print("  macOS (Homebrew): brew install cloudflared", file=sys.stderr)
    print("  Other platforms: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/", file=sys.stderr)
    print("Manual webhook path:", file=sys.stderr)
    print(f"  1. Expose {photon_tunnel.local_url()} with your reverse proxy.", file=sys.stderr)
    print(f"  2. hermes photon webhook register https://YOUR-PUBLIC-URL{photon_tunnel.webhook_path()}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Small interactive helpers

def _prompt(prompt: str, *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        if secret:
            return getpass.getpass(prompt).strip()
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return ""
