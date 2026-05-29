"""Tests for Photon quick-setup reconciliation helpers."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from plugins.platforms.photon import cli as photon_cli


def _ctx(tmp_path: Path, webhook_url: str) -> photon_cli._PhotonSetupContext:
    return photon_cli._PhotonSetupContext(
        args=argparse.Namespace(verbose=False),
        hermes_home=tmp_path,
        env_path=tmp_path / ".env",
        project_name="Hermes Agent",
        webhook_port=8788,
        webhook_path="/photon/webhook",
        project_id="project-id",
        project_secret="project-secret",
        webhook_url=webhook_url,
    )


def _public_health_failure(
    ctx: photon_cli._PhotonSetupContext,
    *,
    step: str,
) -> photon_cli._FailedInvariant:
    return photon_cli._failed_invariant(
        ctx,
        step=step,
        summary="public health failed",
        expected="public health returns ok",
        observed={"ok": False, "detail": "system DNS failed"},
        evidence={"reason": "test"},
        repair="retry",
    )


def test_managed_public_health_failure_recycles_tunnel_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path, "https://old.trycloudflare.com/photon/webhook")
    ctx.local_health = photon_cli._HealthResult(
        True,
        "http://127.0.0.1:8788/healthz",
        "ok",
        status=200,
    )
    wait_calls: list[str] = []
    restart_calls: list[bool] = []
    ensure_gateway_calls: list[str] = []
    webhook_calls: list[str] = []

    def fake_wait_public_health(
        call_ctx: photon_cli._PhotonSetupContext,
        *,
        reason: str,
    ) -> None:
        wait_calls.append(reason)
        if len(wait_calls) == 1:
            raise _public_health_failure(call_ctx, step="public webhook health")
        call_ctx.public_health = (True, "ok")

    def fake_start(**kwargs: Any) -> photon_cli.photon_tunnel.TunnelStartResult:
        assert kwargs["force_new"] is True
        return photon_cli.photon_tunnel.TunnelStartResult(
            success=True,
            public_url="https://new.trycloudflare.com",
            webhook_url="https://new.trycloudflare.com/photon/webhook",
        )

    def fake_webhook_register(
        call_ctx: photon_cli._PhotonSetupContext,
    ) -> photon_cli._WebhookEnsureResult:
        webhook_calls.append(call_ctx.webhook_url)
        return photon_cli._WebhookEnsureResult(
            hooks=[{"id": "new"}],
            registered=True,
            secret_changed=False,
            public_url_changed=True,
        )

    def fake_restart_if_changed(
        call_ctx: photon_cli._PhotonSetupContext,
        *,
        reason: str,
    ) -> None:
        restart_calls.append(call_ctx.runtime_secrets_changed)
        assert reason == "managed tunnel URL changed"
        call_ctx.runtime_secrets_changed = False

    monkeypatch.setattr(photon_cli, "_wait_for_public_health", fake_wait_public_health)
    monkeypatch.setattr(photon_cli.photon_tunnel, "start", fake_start)
    monkeypatch.setattr(photon_cli, "_ensure_current_webhook_registered", fake_webhook_register)
    monkeypatch.setattr(
        photon_cli,
        "_restart_gateway_if_runtime_secrets_changed",
        fake_restart_if_changed,
    )
    monkeypatch.setattr(
        photon_cli,
        "_ensure_gateway_local_runtime",
        lambda call_ctx: ensure_gateway_calls.append(call_ctx.webhook_url),
    )

    photon_cli._wait_for_public_health_with_managed_repair(
        ctx,
        reason="post-webhook verification",
    )

    assert ctx.webhook_url == "https://new.trycloudflare.com/photon/webhook"
    assert ctx.public_health == (True, "ok")
    assert webhook_calls == ["https://new.trycloudflare.com/photon/webhook"]
    assert restart_calls == [True]
    assert ensure_gateway_calls == ["https://new.trycloudflare.com/photon/webhook"]
    assert wait_calls == [
        "post-webhook verification",
        (
            "post-webhook verification; refreshed managed tunnel from "
            "https://old.trycloudflare.com/photon/webhook"
        ),
    ]


def test_managed_dns_failure_recycles_tunnel_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path, "https://old.trycloudflare.com/photon/webhook")
    ctx.local_health = photon_cli._HealthResult(
        True,
        "http://127.0.0.1:8788/healthz",
        "ok",
        status=200,
    )
    failure = _public_health_failure(ctx, step="public webhook DNS")
    wait_calls: list[str] = []
    restart_calls: list[bool] = []
    ensure_gateway_calls: list[str] = []
    webhook_calls: list[str] = []

    def fake_wait_public_health(
        call_ctx: photon_cli._PhotonSetupContext,
        *,
        reason: str,
    ) -> None:
        wait_calls.append(reason)
        if len(wait_calls) == 1:
            raise failure
        call_ctx.public_health = (True, "ok")

    def fake_start(**kwargs: Any) -> photon_cli.photon_tunnel.TunnelStartResult:
        assert kwargs["force_new"] is True
        return photon_cli.photon_tunnel.TunnelStartResult(
            success=True,
            public_url="https://new.trycloudflare.com",
            webhook_url="https://new.trycloudflare.com/photon/webhook",
        )

    def fake_webhook_register(
        call_ctx: photon_cli._PhotonSetupContext,
    ) -> photon_cli._WebhookEnsureResult:
        webhook_calls.append(call_ctx.webhook_url)
        return photon_cli._WebhookEnsureResult(
            hooks=[{"id": "new"}],
            registered=True,
            secret_changed=False,
            public_url_changed=True,
        )

    def fake_restart_if_changed(
        call_ctx: photon_cli._PhotonSetupContext,
        *,
        reason: str,
    ) -> None:
        restart_calls.append(call_ctx.runtime_secrets_changed)
        assert reason == "managed tunnel URL changed"
        call_ctx.runtime_secrets_changed = False

    monkeypatch.setattr(photon_cli, "_wait_for_public_health", fake_wait_public_health)
    monkeypatch.setattr(photon_cli.photon_tunnel, "start", fake_start)
    monkeypatch.setattr(photon_cli, "_ensure_current_webhook_registered", fake_webhook_register)
    monkeypatch.setattr(
        photon_cli,
        "_restart_gateway_if_runtime_secrets_changed",
        fake_restart_if_changed,
    )
    monkeypatch.setattr(
        photon_cli,
        "_ensure_gateway_local_runtime",
        lambda call_ctx: ensure_gateway_calls.append(call_ctx.webhook_url),
    )

    photon_cli._wait_for_public_health_with_managed_repair(
        ctx,
        reason="post-webhook verification",
    )

    assert ctx.webhook_url == "https://new.trycloudflare.com/photon/webhook"
    assert ctx.public_health == (True, "ok")
    assert webhook_calls == ["https://new.trycloudflare.com/photon/webhook"]
    assert restart_calls == [True]
    assert ensure_gateway_calls == ["https://new.trycloudflare.com/photon/webhook"]
    assert wait_calls == [
        "post-webhook verification",
        (
            "post-webhook verification; refreshed managed tunnel from "
            "https://old.trycloudflare.com/photon/webhook"
        ),
    ]


def test_user_owned_public_health_failure_does_not_recycle_tunnel(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path, "https://example.com/photon/webhook")
    ctx.local_health = photon_cli._HealthResult(
        True,
        "http://127.0.0.1:8788/healthz",
        "ok",
        status=200,
    )
    failure = _public_health_failure(ctx, step="public webhook health")

    def fake_wait_public_health(
        _call_ctx: photon_cli._PhotonSetupContext,
        *,
        reason: str,
    ) -> None:
        assert reason == "user-owned URL"
        raise failure

    monkeypatch.setattr(photon_cli, "_wait_for_public_health", fake_wait_public_health)
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "start",
        lambda **_kwargs: pytest.fail("user-owned URLs must not recycle managed tunnels"),
    )

    with pytest.raises(photon_cli._FailedInvariant) as raised:
        photon_cli._wait_for_public_health_with_managed_repair(
            ctx,
            reason="user-owned URL",
        )

    assert raised.value is failure


def test_unowned_photon_sidecar_port_failure_prints_kill_command(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _ctx(tmp_path, "https://example.com/photon/webhook")
    owner = {
        "present": True,
        "port": 8789,
        "command": "node",
        "pid": "17980",
        "user": "patrickruan",
        "ppid": "17978",
        "full_command": (
            "/Users/patrickruan/.local/node/bin/node "
            "/old/hermes/plugins/platforms/photon/sidecar/index.mjs"
        ),
        "hermes_home": "",
        "photon_sidecar_port": "8789",
        "is_photon_sidecar": True,
    }

    monkeypatch.setattr(photon_cli, "_sidecar_port", lambda: 8789)
    monkeypatch.setattr(photon_cli, "_sidecar_port_owner", lambda _port: owner)

    with pytest.raises(photon_cli._FailedInvariant) as raised:
        photon_cli._ensure_sidecar_port_available(ctx)

    failure = raised.value
    assert failure.summary == (
        "Photon sidecar port is already owned by an unowned Photon sidecar"
    )
    assert "kill 17980" in failure.repair
    assert "kill -9 17980" in failure.repair
    assert "lsof -nP -iTCP:8789 -sTCP:LISTEN" in failure.repair
    assert "stop pid" not in failure.repair
