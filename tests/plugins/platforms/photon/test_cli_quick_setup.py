"""Tests for the simplified Photon quick-setup reconciler and commands."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from plugins.platforms.photon import cli as photon_cli


def _ctx(tmp_path: Path) -> photon_cli._PhotonSetupContext:
    return photon_cli._PhotonSetupContext(
        args=argparse.Namespace(verbose=False),
        hermes_home=tmp_path,
        env_path=tmp_path / ".env",
        project_name="Hermes Agent",
        project_id="project-id",
        project_secret="project-secret",
    )


def test_reconciler_runs_invariants_in_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    calls: list[str] = []

    def record(name: str, *, returns: Any = None):
        def _fn(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            return returns
        return _fn

    monkeypatch.setattr(photon_cli, "_ensure_dashboard_auth", record("auth", returns="tok"))
    monkeypatch.setattr(photon_cli, "_ensure_spectrum_project", record("project"))
    monkeypatch.setattr(photon_cli, "_ensure_operator_phone", record("phone"))
    monkeypatch.setattr(photon_cli, "_ensure_sidecar_ready", record("sidecar"))
    monkeypatch.setattr(photon_cli, "_ensure_photon_gateway_platform_enabled", record("enable"))
    monkeypatch.setattr(photon_cli, "_ensure_gateway_running", record("gateway"))
    monkeypatch.setattr(photon_cli, "_wait_for_photon_connected", record("connected"))

    photon_cli._run_quick_setup_reconciler(ctx)

    assert calls == [
        "auth",
        "project",
        "phone",
        "sidecar",
        "enable",
        "gateway",
        "connected",
    ]
    # No webhook/tunnel/active-home/port invariants remain.
    assert not hasattr(photon_cli, "_ensure_public_webhook_path")
    assert not hasattr(photon_cli, "_ensure_active_home_available")
    assert not hasattr(photon_cli, "_ensure_sidecar_port_available")


def test_reset_local_clears_only_runtime_env_keys(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    removed: list[str] = []
    monkeypatch.setattr(photon_cli, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(photon_cli.photon_auth, "_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        photon_cli.photon_auth, "load_project_credentials", lambda: ("pid", "sec")
    )
    monkeypatch.setattr(
        photon_cli, "_remove_env_value", lambda key: (removed.append(key) or True)
    )

    rc = photon_cli._cmd_reset(argparse.Namespace(all=False, scope=None))
    assert rc == 0
    assert set(removed) == set(photon_cli._PHOTON_RUNTIME_RESET_ENV_KEYS)
    # Dashboard token + allowlist survive a local reset.
    assert "PHOTON_DASHBOARD_TOKEN" not in removed


def test_reset_all_clears_auth_and_sender_state(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    removed: list[str] = []
    monkeypatch.setattr(photon_cli, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(photon_cli.photon_auth, "_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        photon_cli.photon_auth, "load_project_credentials", lambda: ("pid", "sec")
    )
    monkeypatch.setattr(photon_cli, "_confirm_reset_all", lambda *_a: True)
    monkeypatch.setattr(
        photon_cli, "_remove_env_value", lambda key: (removed.append(key) or True)
    )

    rc = photon_cli._cmd_reset(argparse.Namespace(all=True, scope=None))
    assert rc == 0
    assert set(removed) == set(photon_cli._PHOTON_ALL_RESET_ENV_KEYS)
    assert "PHOTON_DASHBOARD_TOKEN" in removed


def test_runtime_reset_keys_drop_webhook_entries() -> None:
    joined = " ".join(photon_cli._PHOTON_ALL_RESET_ENV_KEYS)
    assert "WEBHOOK" not in joined
    assert "PHOTON_SIDECAR_PORT" not in photon_cli._PHOTON_GATEWAY_ENV_KEYS


def test_next_status_step_prompts_login_without_credentials(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(photon_cli.photon_auth, "load_photon_token", lambda: None)
    monkeypatch.setattr(
        photon_cli.photon_auth, "load_project_credentials", lambda: ("", "")
    )
    assert photon_cli._next_status_step("✓ installed") == "hermes photon login"


def test_next_status_step_connected_gateway(monkeypatch: Any) -> None:
    monkeypatch.setattr(photon_cli.photon_auth, "load_photon_token", lambda: "tok")
    monkeypatch.setattr(
        photon_cli.photon_auth, "load_project_credentials", lambda: ("pid", "sec")
    )
    monkeypatch.setattr(photon_cli, "_photon_sender_access_configured", lambda: True)
    runtime = {
        "running": True,
        "status": {"platforms": {"photon": {"state": "connected"}}},
    }
    step = photon_cli._next_status_step("✓ installed", runtime_status=runtime)
    assert "send an iMessage" in step
