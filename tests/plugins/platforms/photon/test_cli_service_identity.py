from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from plugins.platforms.photon import cli as photon_cli


def _ctx(tmp_path: Path) -> photon_cli._PhotonSetupContext:
    return photon_cli._PhotonSetupContext(
        args=argparse.Namespace(),
        hermes_home=tmp_path / "current-home",
        env_path=tmp_path / "current-home" / ".env",
        project_name="Hermes Agent",
        webhook_port=8798,
        webhook_path="/photon/webhook",
    )


def test_service_for_current_home_keeps_matching_service(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    service = {
        "manager": "launchd",
        "installed": True,
        "running": True,
        "service_home": str(ctx.hermes_home),
    }

    usable = photon_cli._service_for_current_home(ctx, service)

    assert usable is service
    assert usable["installed"] is True
    assert usable["running"] is True


def test_service_for_current_home_ignores_other_home(
    tmp_path: Path,
    capsys: Any,
) -> None:
    ctx = _ctx(tmp_path)
    service = {
        "manager": "launchd",
        "installed": True,
        "running": True,
        "service_home": str(tmp_path / "default-home"),
    }

    usable = photon_cli._service_for_current_home(ctx, service, announce=True)

    assert usable["ignored"] is True
    assert usable["installed"] is False
    assert usable["running"] is False
    assert usable["ignored_service_home"].endswith("default-home")
    assert usable["current_home"].endswith("current-home")
    out = capsys.readouterr().out
    assert "using a temporary gateway" in out


def test_start_current_home_gateway_uses_detached_for_other_home_service(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    launched: list[photon_cli._PhotonSetupContext] = []
    service = {
        "manager": "launchd",
        "installed": True,
        "running": False,
        "service_home": str(tmp_path / "default-home"),
    }

    monkeypatch.setattr(
        photon_cli,
        "_launch_detached_gateway",
        lambda setup_ctx: launched.append(setup_ctx),
    )

    photon_cli._start_current_home_gateway(ctx, service)

    assert launched == [ctx]


def test_public_health_dns_failure_next_step_is_concrete() -> None:
    step = photon_cli._public_health_next_step(
        (
            False,
            (
                "https://fresh.trycloudflare.com/healthz failed: "
                "socket.gaierror; system DNS failed to resolve "
                "fresh.trycloudflare.com"
            ),
        ),
        {"running": True},
        "https://fresh.trycloudflare.com/photon/webhook",
    )

    assert step is not None
    assert "wait 30-60s" in step
    assert "hermes photon webhook tunnel stop" in step
    assert photon_cli._public_health_can_be_transient("system DNS failed")


def test_sidecar_preflight_stops_orphan_from_other_home(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    stopped: list[str] = []
    monkeypatch.setattr(photon_cli, "_sidecar_port", lambda: 8799)
    monkeypatch.setattr(
        photon_cli,
        "_sidecar_port_owner",
        lambda _port: {
            "present": True,
            "port": 8799,
            "pid": "62495",
            "ppid": "1",
            "command": "node",
            "full_command": (
                "/usr/bin/node "
                "/repo/plugins/platforms/photon/sidecar/index.mjs"
            ),
            "is_photon_sidecar": True,
            "hermes_home": str(tmp_path / "old-home"),
        },
    )
    monkeypatch.setattr(
        photon_cli,
        "_terminate_process",
        lambda pid: stopped.append(pid) or True,
    )

    photon_cli._ensure_sidecar_port_available(ctx)

    assert stopped == ["62495"]


def test_sidecar_preflight_allows_current_home_running_gateway(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(photon_cli, "_sidecar_port", lambda: 8799)
    monkeypatch.setattr(
        photon_cli,
        "_sidecar_port_owner",
        lambda _port: {
            "present": True,
            "port": 8799,
            "pid": "70000",
            "ppid": "69999",
            "is_photon_sidecar": True,
            "hermes_home": str(ctx.hermes_home),
        },
    )
    monkeypatch.setattr(
        photon_cli,
        "_inspect_gateway_runtime",
        lambda: {"running": True},
    )

    photon_cli._ensure_sidecar_port_available(ctx)


def test_sidecar_preflight_fails_unknown_port_owner(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(photon_cli, "_sidecar_port", lambda: 8799)
    monkeypatch.setattr(
        photon_cli,
        "_sidecar_port_owner",
        lambda _port: {
            "present": True,
            "port": 8799,
            "pid": "42",
            "command": "python",
            "full_command": "python -m http.server",
            "is_photon_sidecar": False,
        },
    )

    try:
        photon_cli._ensure_sidecar_port_available(ctx)
    except photon_cli._FailedInvariant as exc:
        assert exc.step == "sidecar port"
        assert "already in use" in exc.summary
        assert exc.observed["pid"] == "42"
    else:
        raise AssertionError("expected sidecar port conflict")


def test_sidecar_preflight_fails_same_home_orphan(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(photon_cli, "_sidecar_port", lambda: 8799)
    monkeypatch.setattr(
        photon_cli,
        "_sidecar_port_owner",
        lambda _port: {
            "present": True,
            "port": 8799,
            "pid": "70000",
            "ppid": "1",
            "is_photon_sidecar": True,
            "hermes_home": str(ctx.hermes_home),
        },
    )
    monkeypatch.setattr(
        photon_cli,
        "_inspect_gateway_runtime",
        lambda: {"running": False},
    )

    try:
        photon_cli._ensure_sidecar_port_available(ctx)
    except photon_cli._FailedInvariant as exc:
        assert exc.step == "sidecar port"
        assert "stale sidecar" in exc.summary
    else:
        raise AssertionError("expected stale same-home sidecar failure")
