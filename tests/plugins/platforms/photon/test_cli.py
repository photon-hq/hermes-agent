"""Tests for Photon CLI helpers."""
from __future__ import annotations

import json
import argparse
from pathlib import Path
from typing import Any

import pytest

import gateway.status as gateway_status
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


def _quick_ctx(tmp_path: Path) -> photon_cli._PhotonSetupContext:
    args = argparse.Namespace(
        project_name=None,
        phone="+15551234567",
        first_name=None,
        last_name=None,
        email=None,
        no_browser=True,
        new_project=False,
        skip_sidecar_install=False,
    )
    return photon_cli._PhotonSetupContext(
        args=args,
        hermes_home=tmp_path,
        env_path=tmp_path / ".env",
        project_name="Hermes Agent",
        webhook_port=8788,
        webhook_path="/photon/webhook",
    )


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


def test_quick_setup_missing_dashboard_token_runs_login(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    calls: list[str] = []
    token_reads = iter([None, "token"])

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_photon_token",
        lambda: next(token_reads),
    )
    monkeypatch.setattr(
        photon_cli,
        "_cmd_login",
        lambda _args: calls.append("login") or 0,
    )
    monkeypatch.setattr(
        photon_cli.photon_auth,
        "validate_photon_token",
        lambda token: calls.append(f"validate:{token}") or {"id": "user"},
    )

    token = photon_cli._ensure_dashboard_auth(ctx)

    assert token == "token"
    assert calls == ["login", "validate:token"]


def test_quick_setup_invalid_dashboard_token_clears_and_runs_login(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    calls: list[str] = []
    token_reads = iter(["bad-token", "good-token"])

    def fake_validate(token: str) -> dict[str, str]:
        calls.append(f"validate:{token}")
        if token == "bad-token":
            raise photon_cli.photon_auth.PhotonDashboardAuthError("rejected")
        return {"id": "user"}

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_photon_token",
        lambda: next(token_reads),
    )
    monkeypatch.setattr(
        photon_cli.photon_auth,
        "clear_photon_token",
        lambda: calls.append("clear") or True,
    )
    monkeypatch.setattr(
        photon_cli,
        "_cmd_login",
        lambda _args: calls.append("login") or 0,
    )
    monkeypatch.setattr(photon_cli.photon_auth, "validate_photon_token", fake_validate)

    token = photon_cli._ensure_dashboard_auth(ctx)

    assert token == "good-token"
    assert calls == [
        "validate:bad-token",
        "clear",
        "login",
        "validate:good-token",
    ]


class _HTTPStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = type("Response", (), {"status_code": status_code})()


def test_quick_setup_rejected_dashboard_token_clears_and_runs_login(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    calls: list[str] = []
    token_reads = iter(["bad-token", "good-token"])

    def fake_validate(token: str) -> dict[str, str]:
        calls.append(f"validate:{token}")
        if token == "bad-token":
            raise _HTTPStatusError(401)
        return {"id": "user"}

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_photon_token",
        lambda: next(token_reads),
    )
    monkeypatch.setattr(
        photon_cli.photon_auth,
        "clear_photon_token",
        lambda: calls.append("clear") or True,
    )
    monkeypatch.setattr(
        photon_cli,
        "_cmd_login",
        lambda _args: calls.append("login") or 0,
    )
    monkeypatch.setattr(photon_cli.photon_auth, "validate_photon_token", fake_validate)

    token = photon_cli._ensure_dashboard_auth(ctx)

    assert token == "good-token"
    assert calls == [
        "validate:bad-token",
        "clear",
        "login",
        "validate:good-token",
    ]


def test_quick_setup_spectrum_401_clears_cached_project_credentials(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    calls: list[str] = []
    hook_calls = iter([_HTTPStatusError(401), []])

    def fake_list_webhooks(*_args: Any, **_kwargs: Any) -> list:
        result = next(hook_calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "load_project_credentials",
        lambda: ("old-project", "old-secret"),
    )
    monkeypatch.setattr(photon_cli.photon_auth, "list_webhooks", fake_list_webhooks)
    monkeypatch.setattr(
        photon_cli,
        "_clear_local_project_runtime_state",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(
        photon_cli,
        "_resolve_setup_project",
        lambda *_args, **_kwargs: ("new-project", "new-secret"),
    )

    photon_cli._ensure_spectrum_project(ctx, "token")

    assert calls == ["clear"]
    assert ctx.project_id == "new-project"
    assert ctx.project_secret == "new-secret"


def test_active_home_mismatch_stops_quick_setup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "active_home_mismatch",
        lambda: ("/other/.hermes", str(tmp_path)),
    )
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "active_home_path",
        lambda: tmp_path / "active-home.json",
    )
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "active_home_record",
        lambda: {"hermes_home": "/other/.hermes"},
    )

    with pytest.raises(photon_cli._FailedInvariant) as exc:
        photon_cli._ensure_active_home_available(ctx)

    assert "another Hermes home" in exc.value.summary
    assert exc.value.evidence["hermes_home"] == str(tmp_path)


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


def test_next_step_does_not_block_on_unowned_stale_cleanup(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [
        _hook("current", current_url),
        _hook("unowned-stale", "https://foreign-old.trycloudflare.com/photon/webhook"),
    ]
    _stub_ready_next_step_dependencies(monkeypatch, public_url=current_url)
    monkeypatch.setattr(photon_cli.photon_tunnel, "owned_webhook_ids", lambda: set())
    monkeypatch.setattr(gateway_status, "is_gateway_running", lambda: False)

    step = photon_cli._next_status_step(
        "✓ installed",
        {"running": True},
        registered_hooks=hooks,
        public_health=(True, "https://current.trycloudflare.com/healthz"),
    )

    assert step == "hermes gateway run -v  (or `hermes gateway restart` if already running)"


def test_next_step_prioritizes_public_health_over_unowned_stale_cleanup(
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
        public_health=(
            False,
            "https://current.trycloudflare.com/healthz failed: HTTP Error 502: Bad Gateway",
        ),
    )

    assert step == "hermes gateway restart  (then re-run `hermes photon status`)"


def test_next_step_does_not_suggest_blind_restart_when_local_health_fails(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [_hook("current", current_url)]
    _stub_ready_next_step_dependencies(monkeypatch, public_url=current_url)
    monkeypatch.setattr(photon_cli.photon_tunnel, "owned_webhook_ids", lambda: set())

    step = photon_cli._next_status_step(
        "✓ installed",
        {"running": True},
        registered_hooks=hooks,
        public_health=(
            False,
            "https://current.trycloudflare.com/healthz failed: HTTP Error 502: Bad Gateway",
        ),
        local_health=photon_cli._HealthResult(
            ok=False,
            url="http://127.0.0.1:8788/healthz",
            detail="ConnectionRefusedError",
        ),
    )

    assert step == "start or repair the current-home gateway; local health is failing"


def test_service_home_mismatch_reports_exact_invariant(
    tmp_path: Path,
) -> None:
    ctx = _quick_ctx(tmp_path)
    service = {
        "manager": "launchd",
        "installed": True,
        "service_home": str(tmp_path / "other"),
        "expected_home": str(tmp_path),
    }

    with pytest.raises(photon_cli._FailedInvariant) as exc:
        photon_cli._fail_if_service_home_mismatch(ctx, service)

    assert exc.value.step == "gateway service identity"
    assert "another Hermes home" in exc.value.summary


def test_runtime_project_mismatch_reports_exact_invariant(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    ctx.project_id = "5123d23c-8642-44e4-acf1-66695c1b8171"
    runtime = {
        "running": True,
        "pid": 123,
        "status": {
            "platforms": {
                "photon": {
                    "state": "paused",
                    "error_message": (
                        "Client error '401 Unauthorized' for url "
                        "'https://spectrum.photon.codes/projects/"
                        "9a100a97-8602-4a79-bf80-2597680a2b83/webhooks/'"
                    ),
                },
            },
        },
    }
    monkeypatch.setattr(
        photon_cli,
        "_inspect_gateway_service_identity",
        lambda _ctx: {"manager": "launchd", "service_home": str(tmp_path)},
    )

    with pytest.raises(photon_cli._FailedInvariant) as exc:
        photon_cli._assert_runtime_project_matches(ctx, runtime)

    assert exc.value.step == "gateway runtime project identity"
    assert exc.value.observed["setup_project_id"] == ctx.project_id
    assert (
        exc.value.observed["gateway_project_id"]
        == "9a100a97-8602-4a79-bf80-2597680a2b83"
    )


def test_runtime_project_id_prefers_runtime_metadata() -> None:
    runtime = {
        "status": {
            "platforms": {
                "photon": {
                    "project_id": "5123d23c-8642-44e4-acf1-66695c1b8171",
                    "error_message": (
                        "https://spectrum.photon.codes/projects/"
                        "9a100a97-8602-4a79-bf80-2597680a2b83/webhooks/"
                    ),
                },
            },
        },
    }

    assert (
        photon_cli._runtime_photon_project_id(runtime)
        == "5123d23c-8642-44e4-acf1-66695c1b8171"
    )


def test_failure_log_tail_collects_relevant_redacted_lines(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "gateway.log").write_text(
        "\n".join([
            "ordinary startup line",
            "[photon] active Hermes home: /tmp/hermes",
            "Authorization: Bearer secret-token",
            "Photon failed with PHOTON_PROJECT_SECRET=secret-value",
        ]),
        encoding="utf-8",
    )
    (log_dir / "errors.log").write_text("Traceback: photon exploded\n", encoding="utf-8")
    cloudflared_log = tmp_path / "cloudflared.log"
    cloudflared_log.write_text("cloudflared tunnel ready\n", encoding="utf-8")
    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "log_path",
        lambda: cloudflared_log,
    )

    logs = photon_cli._collect_relevant_log_tail(ctx)

    rendered = "\n".join(line for lines in logs.values() for line in lines)
    assert "[photon] active Hermes home" in rendered
    assert "Traceback: photon exploded" in rendered
    assert "cloudflared tunnel ready" in rendered
    assert "secret-token" not in rendered
    assert "secret-value" not in rendered


def test_webhook_reconcile_refuses_to_delete_unowned_current_webhook_without_secret(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ctx = _quick_ctx(tmp_path)
    ctx.project_id = "project"
    ctx.project_secret = "secret"
    ctx.webhook_url = "https://current.trycloudflare.com/photon/webhook"
    deleted: list[str] = []

    monkeypatch.setattr(
        photon_cli.photon_auth,
        "list_webhooks",
        lambda *_args, **_kwargs: [_hook("foreign", ctx.webhook_url)],
    )
    monkeypatch.setattr(photon_cli, "_webhook_secret_present", lambda: False)
    monkeypatch.setattr(photon_cli.photon_tunnel, "owned_webhook_ids", lambda: set())
    monkeypatch.setattr(
        photon_cli.photon_auth,
        "delete_webhook",
        lambda *_args, **kwargs: deleted.append(kwargs["webhook_id"]),
    )

    with pytest.raises(photon_cli._FailedInvariant) as exc:
        photon_cli._ensure_current_webhook_registered(ctx)

    assert "local signing secret is missing" in exc.value.summary
    assert deleted == []


def test_next_step_repairs_unresolvable_public_health_with_new_tunnel(
    monkeypatch: Any,
) -> None:
    current_url = "https://current.trycloudflare.com/photon/webhook"
    hooks = [_hook("current", current_url)]
    _stub_ready_next_step_dependencies(monkeypatch, public_url=current_url)

    step = photon_cli._next_status_step(
        "✓ installed",
        {"running": True},
        registered_hooks=hooks,
        public_health=(
            False,
            "https://current.trycloudflare.com/healthz failed: "
            "<urlopen error [Errno 8] nodename nor servname provided, or not known>",
        ),
    )

    assert step == "hermes photon webhook tunnel stop && hermes photon webhook tunnel start"


def test_public_health_status_retries_transient_bad_gateway(
    monkeypatch: Any,
) -> None:
    calls = iter([
        (False, "https://current.trycloudflare.com/healthz failed: HTTP Error 502: Bad Gateway"),
        (True, "https://current.trycloudflare.com/healthz"),
    ])
    sleeps: list[int] = []

    monkeypatch.setattr(
        photon_cli.photon_tunnel,
        "check_public_health",
        lambda _url: next(calls),
    )
    monkeypatch.setattr(photon_cli.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = photon_cli._check_public_health_for_status(
        "https://current.trycloudflare.com/photon/webhook"
    )

    assert result == (True, "https://current.trycloudflare.com/healthz")
    assert sleeps == [2]


def test_public_health_status_does_not_retry_unresolvable_host(
    monkeypatch: Any,
) -> None:
    calls = 0
    failure = (
        False,
        "https://current.trycloudflare.com/healthz failed: "
        "<urlopen error [Errno 8] nodename nor servname provided, or not known>",
    )

    def fake_check(_url: str) -> tuple[bool, str]:
        nonlocal calls
        calls += 1
        return failure

    monkeypatch.setattr(photon_cli.photon_tunnel, "check_public_health", fake_check)
    monkeypatch.setattr(photon_cli.time, "sleep", lambda _seconds: None)

    result = photon_cli._check_public_health_for_status(
        "https://current.trycloudflare.com/photon/webhook"
    )

    assert result == failure
    assert calls == 1
