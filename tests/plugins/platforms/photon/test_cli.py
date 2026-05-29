"""Tests for Photon CLI helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_cli import cli_output
from plugins.platforms.photon import cli as photon_cli


class _Proc:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _with_node_modules(tmp_path: Path, monkeypatch: Any) -> Path:
    sidecar = tmp_path / "sidecar"
    (sidecar / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(photon_cli, "_SIDECAR_DIR", sidecar)
    monkeypatch.setattr(photon_cli.shutil, "which", lambda _name: "/usr/bin/npm")
    return sidecar


def test_sidecar_dependency_status_missing_node_modules(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(photon_cli, "_SIDECAR_DIR", tmp_path / "sidecar")

    status = photon_cli._sidecar_dependency_status()

    assert "hermes photon install-sidecar" in status


def test_sidecar_dependency_status_rejects_old_spectrum_ts(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    _with_node_modules(tmp_path, monkeypatch)

    def fake_run(*_args: Any, **_kwargs: Any) -> _Proc:
        return _Proc(
            returncode=0,
            stdout=json.dumps({
                "dependencies": {
                    "spectrum-ts": {"version": "0.1.2"},
                },
            }),
        )

    monkeypatch.setattr(photon_cli.subprocess, "run", fake_run)

    status = photon_cli._sidecar_dependency_status()

    assert "spectrum-ts 0.1.2 is too old" in status


def test_sidecar_dependency_status_surfaces_npm_problems(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    _with_node_modules(tmp_path, monkeypatch)

    def fake_run(*_args: Any, **_kwargs: Any) -> _Proc:
        return _Proc(
            returncode=1,
            stdout=json.dumps({
                "dependencies": {
                    "spectrum-ts": {"version": "1.7.2"},
                },
                "problems": ["invalid: spectrum-ts@1.7.2 from the root project"],
            }),
        )

    monkeypatch.setattr(photon_cli.subprocess, "run", fake_run)

    status = photon_cli._sidecar_dependency_status()

    assert "npm reports invalid: spectrum-ts@1.7.2" in status


def test_sidecar_dependency_status_accepts_current_spectrum_ts(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    _with_node_modules(tmp_path, monkeypatch)

    def fake_run(*_args: Any, **_kwargs: Any) -> _Proc:
        return _Proc(
            returncode=0,
            stdout=json.dumps({
                "dependencies": {
                    "spectrum-ts": {"version": "1.7.2"},
                },
            }),
        )

    monkeypatch.setattr(photon_cli.subprocess, "run", fake_run)

    status = photon_cli._sidecar_dependency_status()

    assert status == "✓ installed (spectrum-ts 1.7.2)"


def test_interactive_setup_skips_quick_setup_when_already_configured(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    calls: list[bool] = []

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_project_credentials",
        lambda: ("pid", "secret"),
    )
    monkeypatch.setattr(photon_cli.photon_auth, "load_photon_token", lambda: None)
    monkeypatch.setattr(
        photon_cli,
        "_interactive_setup_already_configured",
        lambda: True,
    )
    monkeypatch.setattr(cli_output, "prompt_yes_no", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        photon_cli,
        "_cmd_quick_setup",
        lambda _args: calls.append(True) or 0,
    )

    photon_cli.interactive_setup()

    assert calls == []
    assert "Photon iMessage is already configured" in capsys.readouterr().out


def test_interactive_setup_runs_quick_setup_when_reconfigure_confirmed(
    monkeypatch: Any,
) -> None:
    calls: list[bool] = []

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_project_credentials",
        lambda: ("pid", "secret"),
    )
    monkeypatch.setattr(photon_cli.photon_auth, "load_photon_token", lambda: None)
    monkeypatch.setattr(
        photon_cli,
        "_interactive_setup_already_configured",
        lambda: True,
    )
    monkeypatch.setattr(cli_output, "prompt_yes_no", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        photon_cli,
        "_cmd_quick_setup",
        lambda _args: calls.append(True) or 0,
    )

    photon_cli.interactive_setup()

    assert calls == [True]


def _hook(webhook_id: str, url: str) -> dict[str, str]:
    return {"id": webhook_id, "webhookUrl": url}


def test_registered_webhook_status_splits_owned_and_unowned_stale(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [
        _hook("current", current_url),
        _hook("owned-stale", "https://owned-old.trycloudflare.com/photon/webhook"),
        _hook("unowned-stale", "https://foreign-old.trycloudflare.com/photon/webhook"),
    ]
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "owned_webhook_ids",
        lambda: {"owned-stale"},
    )

    status = photon_cli._format_registered_webhook_status(
        hooks,
        "",
        current_url,
    )

    assert "current URL registered" in status
    assert "1 owned stale managed" in status
    assert "1 unowned stale managed" in status


def _stub_ready_next_step_dependencies(
    monkeypatch: Any,
    *,
    public_url: str,
) -> None:
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "active_home_mismatch",
        lambda: None,
    )
    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_project_credentials",
        lambda: ("pid", "secret"),
    )
    monkeypatch.setattr(photon_cli.photon_auth, "load_photon_token", lambda: "token")
    monkeypatch.setattr(photon_cli, "_webhook_secret_present", lambda: True)
    monkeypatch.setattr(
        photon_cli,
        "_get_env_value",
        lambda key: public_url if key == "PHOTON_WEBHOOK_PUBLIC_URL" else None,
    )
    monkeypatch.setattr(photon_cli, "_photon_sender_access_configured", lambda: True)


def test_next_step_cleans_owned_stale_managed_webhooks(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [
        _hook("current", current_url),
        _hook("owned-stale", "https://owned-old.trycloudflare.com/photon/webhook"),
    ]
    _stub_ready_next_step_dependencies(monkeypatch, public_url=current_url)
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "owned_webhook_ids",
        lambda: {"owned-stale"},
    )

    step = photon_cli._next_status_step(
        "✓ installed",
        {"running": True},
        registered_hooks=hooks,
    )

    assert step == "hermes photon webhook tunnel start  (cleans owned stale managed webhooks)"


def test_next_step_does_not_claim_unowned_stale_cleanup(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [
        _hook("current", current_url),
        _hook("unowned-stale", "https://foreign-old.trycloudflare.com/photon/webhook"),
    ]
    _stub_ready_next_step_dependencies(monkeypatch, public_url=current_url)
    monkeypatch.setattr(photon_cli.photon_tunnel, "owned_webhook_ids", lambda: set())

    step = photon_cli._next_status_step(
        "✓ installed",
        {"running": True},
        registered_hooks=hooks,
    )

    assert step == "hermes photon webhook list  (delete unowned stale managed webhooks manually)"
