"""``hermes photon ...`` CLI subcommands (registered via ``ctx.register_cli_command()``):
setup (Photon 2.0 project + API key + iMessage line) and status. Setup reuses the project
credentials already configured (``PHOTON_PROJECT_ID`` + ``PHOTON_API_KEY``, e.g. set by hand with
no browser) and signs in with Photon's device login only for the steps that need the account:
finding or creating the project, minting its key and assigning a line.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
from typing import Callable, Optional

from hermes_cli.colors import Colors, color

from . import auth as photon_auth
from hermes_constants import display_hermes_home


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire up `hermes photon ...` subcommands."""
    subs = parser.add_subparsers(dest="photon_command", required=False)
    p_setup = subs.add_parser("setup", help="Connect Photon 2.0 (project + API key + iMessage line)")
    p_setup.add_argument("--project-name", default=None, help="Project name (default: 'Hermes Agent')")
    p_setup.add_argument("--phone", default=None, help="Your E.164 phone number (e.g. +15551234567)")
    p_setup.add_argument("--first-name", default=None)
    p_setup.add_argument("--last-name", default=None)
    p_setup.add_argument("--email", default=None, help="Email for Photon's line invite (optional)")
    p_setup.add_argument("--no-browser", action="store_true",
                         help="Don't try to open a browser for device login; print the URL only")
    p_setup.add_argument("--skip-sidecar-install", action="store_true", help=argparse.SUPPRESS)
    subs.add_parser("status", help="Show the Photon project, API key and iMessage line state")
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    sub = getattr(args, "photon_command", None)
    handler = _cmd_status if sub is None else _COMMANDS.get(sub)  # no subcommand — status by default
    if handler is None:
        print(f"unknown subcommand: {sub}", file=sys.stderr)
        return 2
    return handler(args)


# -- Subcommand handlers -------------------------------------------------------------

def _device_login(args: argparse.Namespace) -> str:
    """Run Photon's RFC 8628 device login and return the account access token."""
    def _show_code(code: photon_auth.DeviceCode) -> None:
        print("\n┌─ Photon device login ────────────────────────────────────────\n"
              f"│  Open this URL:  {code.verification_uri_complete or code.verification_uri}\n"
              f"│  Enter the code: {code.user_code}\n"
              "│  (waiting for approval — Ctrl-C to cancel)\n"
              "└──────────────────────────────────────────────────────────────\n")
    print(f"  Sign in to Photon 2.0 (no account yet? create one at {photon_auth.DASHBOARD_URL})")
    token = photon_auth.login_device_flow(open_browser=not args.no_browser, on_user_code=_show_code)
    email = photon_auth.signed_in_email()
    # Never print any portion of the token (shoulder-surfing / screen recordings).
    print(f"  ✓ signed in{' as ' + email if email else ''}")
    return token


def _account_session(args: argparse.Namespace) -> Callable[[], str]:
    """The account token, fetched on first use: the stored device-login session when Photon
    still honours it, else a fresh device login. Steps that the project key covers never ask."""
    token: Optional[str] = None

    def get() -> str:
        nonlocal token
        if token is None:
            token = photon_auth.load_account_token() or _device_login(args)
        return token
    return get


def _pick_organization(token: str, preferred: Optional[str]) -> str:
    """The organization to find or create the project in: the one used before, the only one,
    or the operator's pick (owned organizations first; non-interactive runs take the first)."""
    orgs = photon_auth.list_organizations(token)
    if not orgs:
        raise photon_auth.PhotonAuthError(
            f"this Photon account has no organization yet; finish signing up at {photon_auth.DASHBOARD_URL}, "
            "then run setup again")
    if preferred and any(o["organization_id"] == preferred for o in orgs):
        return preferred
    if len(orgs) == 1:
        return orgs[0]["organization_id"]
    orgs.sort(key=lambda o: not o["owned"])
    choice = ""
    if sys.stdin.isatty():
        print("  Your Photon organizations:")
        for i, org in enumerate(orgs, 1):
            print(f"    {i}. {org['name']}{' (owner)' if org['owned'] else ''}")
        choice = _prompt("  Create the project in which organization? [1]: ")
    org = orgs[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(orgs) else orgs[0]
    print(f"  Using organization '{org['name']}'")
    return org["organization_id"]


def _setup_project(args: argparse.Namespace, account: Callable[[], str]) -> tuple[str, str]:
    """[1/3] The project id and API key the gateway runs with. A configured key Photon still
    accepts is reused (a new one would break a running gateway until restart); otherwise the
    account finds or creates the project and mints a key, replacing the previous Hermes key."""
    project_id, api_key = photon_auth.load_runtime_credentials()
    if project_id and api_key and not photon_auth.project_key_rejected(project_id, api_key):
        print(f"[1/3] Using the configured Photon project {project_id}")
        photon_auth.archive_legacy_credentials()
        return project_id, api_key
    print("[1/3] Photon refused the configured API key — minting a new one" if project_id and api_key
          else "[1/3] Connecting a Photon project...")
    token = account()
    record = photon_auth.load_project_record()
    name = args.project_name or photon_auth.DEFAULT_PROJECT_NAME
    organization_id = record.get("organization_id")
    if not (project_id and photon_auth.project_exists(token, project_id)):
        organization_id = _pick_organization(token, organization_id)
        project_id = photon_auth.find_project_by_name(token, organization_id, name)
        if project_id:
            print(f"  ✓ found project '{name}' ({project_id})")
        else:
            project_id = photon_auth.create_project(token, organization_id, name)
            print(f"  ✓ created project '{name}' ({project_id})")
    api_key_id, api_key = photon_auth.mint_project_key(token, project_id)
    previous_key_id = record.get("api_key_id") if record.get("project_id") == project_id else None
    if previous_key_id and previous_key_id != api_key_id:
        try:
            photon_auth.revoke_project_key(token, project_id, previous_key_id)
        except photon_auth.PhotonError as e:
            print(f"      (could not revoke the previous Hermes API key: {e})", file=sys.stderr)
    if photon_auth.archive_legacy_credentials():
        print("  ✓ moved the Photon Spectrum credentials into auth.json for Photon's migration tool")
    photon_auth.store_project_credentials(
        project_id=project_id, api_key=api_key, organization_id=organization_id,
        api_key_id=api_key_id, name=name)
    print(f"  ✓ API key saved (PHOTON_PROJECT_ID + PHOTON_API_KEY in {display_hermes_home()}/.env)\n"
          "  ⚠ If the gateway is running, restart it to pick up the new key:  hermes gateway restart")
    return project_id, api_key


def _assign_line(args: argparse.Namespace, account: Callable[[], str], project_id: str,
                 api_key: str, phone: str) -> Optional[dict]:
    """[2/3] Give ``phone`` a shared iMessage line (idempotent). The project key can look an
    assignment up; creating one takes a seat, so it falls back to the account. None when the
    project has a dedicated line instead."""
    print(f"      Assigning an iMessage line to {phone}...")
    if photon_auth.get_imessage_mode(api_key, project_id) == "dedicated":
        print(f"  ✓ this project has a dedicated iMessage line; its number is in the dashboard ({photon_auth.DASHBOARD_URL})")
        return None
    details = {"phone": phone, "first_name": args.first_name, "last_name": args.last_name, "email": args.email}
    try:
        assignment, created = photon_auth.ensure_line_assignment(api_key, project_id, **details)
    except photon_auth.ApiError as e:
        if e.status != 403:
            raise
        assignment, created = photon_auth.ensure_line_assignment(account(), project_id, **details)
    print("  ✓ iMessage line assigned" if created else "  ✓ iMessage line already assigned")
    return assignment


_FIRST_TEXT_WAIT_S = 300


def _wait_for_first_text(project_id: str, api_key: str, phone: str, assignment: dict) -> bool:
    """A shared line can only reply to and start conversations with a number that has texted
    it. Name that one manual step and, on a TTY, wait (bounded) for it. True once it landed."""
    line, since = assignment["assigned_phone_number"], assignment["created_at"]
    with contextlib.suppress(photon_auth.PhotonError):  # a failed check just means we ask and wait
        if photon_auth.has_texted_line(api_key, project_id, phone, since):
            print("  ✓ you've texted your line — routing is live")
            return True
    print("\n" + color("      One manual step: text your agent", Colors.CYAN, Colors.BOLD))
    print("      Photon's shared line can only message you after you've messaged it:\n"
          f"        1. Open Messages on {phone}.\n"
          f"        2. Text {line} — one message of any content is enough.")
    if not sys.stdin.isatty():  # scripted setup / CI: nobody can answer the poll
        print("      Non-interactive run — skipping the wait. Verify later with `hermes photon status`.")
        return False
    print(f"      Setup waits here (up to {_FIRST_TEXT_WAIT_S // 60} minutes) and continues once it lands.\n"
          "      (Ctrl-C to skip — re-running setup resumes where it left off.)")

    def _on_waiting(attempt: int) -> None:
        if attempt % 6 == 0:
            print("      ... still waiting for your message to land")
    try:
        texted = photon_auth.wait_for_first_text(
            api_key, project_id, phone, since, timeout_s=_FIRST_TEXT_WAIT_S, on_waiting=_on_waiting)
        why = f"No message seen within {_FIRST_TEXT_WAIT_S // 60} minutes."
    except KeyboardInterrupt:
        texted, why = False, "Wait skipped (Ctrl-C)."
        print()
    if not texted:
        print(f"      ⚠ {why}\n        Text {line} once from {phone}, then check with `hermes photon status`.")
        return False
    print("  ✓ message received — routing is live")
    return True


def _show_agent_number(number: str) -> None:
    print("\n" + "\n".join((
        color("┌─ Your agent's iMessage number ───────────────────────────────", Colors.GREEN),
        color("│  📱 ", Colors.GREEN) + color(number, Colors.GREEN, Colors.BOLD),
        color("│  Text this number from your phone to talk to your agent.", Colors.GREEN),
        color("└──────────────────────────────────────────────────────────────", Colors.GREEN))))


def _cmd_setup(args: argparse.Namespace) -> int:
    if not photon_auth.ensure_sdk():
        print("Photon's Python client (the `photon` extra) could not be installed", file=sys.stderr)
        return 1
    if photon_auth.has_legacy_credentials():
        print("This Hermes is set up for Photon Spectrum, the platform before Photon 2.0. Setup connects\n"
              "Photon 2.0 and keeps your Spectrum credentials in auth.json for Photon's migration tool.\n")
    # Photon refusals, device-login failures (RuntimeError, TimeoutError) and storage writes (OSError).
    step_errors = (photon_auth.PhotonError, RuntimeError, OSError)
    account = _account_session(args)
    try:
        project_id, api_key = _setup_project(args, account)
    except step_errors as e:
        print(f"project setup failed: {e}", file=sys.stderr)
        return 1
    stored_phone, _ = photon_auth.load_user_numbers()
    phone = args.phone or _prompt(color(
        "[2/3] Your iMessage phone number (E.164, e.g. +15551234567)"
        + (f" [{stored_phone}]" if stored_phone else "") + ": ", Colors.CYAN)) or stored_phone
    routing_live = False
    line = None
    if not phone:
        print("      Skipped the iMessage line (no phone given). Re-run with --phone later.")
    else:
        try:  # name/email are optional and never prompted for (--first-name / --email)
            assignment = _assign_line(args, account, project_id, api_key, phone)
        except ValueError as e:
            print(f"      invalid phone number: {e}", file=sys.stderr)
            return 1
        except step_errors as e:
            print(f"      line assignment failed: {e}", file=sys.stderr)
            return 1
        if assignment is None:
            routing_live = True
        else:
            line = assignment["assigned_phone_number"]
            _show_agent_number(line)
            try:
                photon_auth.store_user_numbers(
                    phone_number=phone, assigned_phone_number=line,
                    assigned_at=assignment["created_at"], project_id=project_id)
            except OSError as e:
                print(f"      (could not save Photon status metadata: {e})", file=sys.stderr)
            routing_live = _wait_for_first_text(project_id, api_key, phone, assignment)
        # Otherwise the gateway denies the operator's own inbound and has no cron home space.
        _autoconfigure_access(phone)
    # [3/3] Enable the platform in config.yaml, or the channel silently stays offline.
    from hermes_cli.config import write_platform_config_field
    write_platform_config_field("photon", "enabled", True, raw=True)
    print("[3/3] ✓ photon platform enabled in config.yaml")
    if not phone:  # saved, but nothing can route until a number has a line
        print("\n⚠ Photon setup saved, but iMessage routing is NOT live: no phone number has a line.\n"
              "  Finish with:  hermes photon setup --phone +15551234567")
        return 0
    if not routing_live:  # saved, but the line cannot reach this number until it texts in
        print(f"\n⚠ Photon setup saved, but iMessage routing is NOT live: text {line or 'your assigned line'} "
              f"once from {phone}.\n  Check with `hermes photon status`.")
        return 0
    print("\n✓ Photon setup complete.\n  Start the gateway:  hermes gateway start")
    return 0


def _autoconfigure_access(phone: str) -> None:
    """Set PHOTON_ALLOWED_USERS and PHOTON_HOME_CHANNEL to the operator's number, each only
    when unset so a hand-tuned value is never clobbered on re-run. A Photon Spectrum chat GUID
    (``any;-;+1555…``) in PHOTON_HOME_CHANNEL is replaced: Photon 2.0 cannot deliver to it."""
    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:
        return
    for key, label in (("PHOTON_ALLOWED_USERS", "allowlisted your number"),
                       ("PHOTON_HOME_CHANNEL", "set your DM as the cron home channel")):
        try:
            current = get_env_value(key)
            spectrum_guid = key == "PHOTON_HOME_CHANNEL" and ";" in (current or "")
            if current and not spectrum_guid:
                print(f"      {key} already set — leaving it as-is.")
                continue
            save_env_value(key, phone)
            print(f"  ✓ {label} ({key})" + (", replacing its Photon Spectrum chat id" if spectrum_guid else ""))
        except Exception as e:
            print(f"      could not set {key}: {e}", file=sys.stderr)


def _cmd_status(_args: argparse.Namespace) -> int:
    phone, assigned = photon_auth.load_user_numbers()
    if phone and not assigned:
        try:
            photon_auth.refresh_user_numbers()
        except (photon_auth.PhotonError, RuntimeError, OSError) as e:
            print(f"      (could not refresh the Photon line: {e})", file=sys.stderr)
    # auth's emit callbacks are the only sinks that see credential-derived strings
    # (keeps cli.py taint-free for CodeQL).
    photon_auth.print_credential_summary(print)
    photon_auth.print_routing_status(print)
    return 0


_COMMANDS = {"setup": _cmd_setup, "status": _cmd_status}


def gateway_setup() -> None:
    """Run Photon first-time setup from the unified `hermes gateway setup` wizard (same flow
    as ``hermes photon setup``; phone is prompted when stdin is a TTY)."""
    _cmd_setup(argparse.Namespace(
        photon_command="setup", project_name=None, phone=None, first_name=None, last_name=None,
        email=None, no_browser=False))


def _prompt(prompt: str, *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return ""
