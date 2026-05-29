"""Tests for Photon managed tunnel helpers."""
from __future__ import annotations

import json
import socket
import urllib.error
from pathlib import Path
from typing import Any

from plugins.platforms.photon import tunnel as photon_tunnel


def test_parse_quick_tunnel_url_returns_latest_match() -> None:
    text = """
    https://old-one.trycloudflare.com
    unrelated output
    https://new-one.trycloudflare.com
    """

    assert photon_tunnel.parse_quick_tunnel_url(text) == (
        "https://new-one.trycloudflare.com"
    )


def test_start_ignores_old_log_urls(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    state_dir = tmp_path / "photon"
    state_dir.mkdir()
    state_path = state_dir / "tunnel.json"
    log_path = state_dir / "cloudflared.log"
    old_url = "https://old-one.trycloudflare.com"
    new_url = "https://new-one.trycloudflare.com"
    log_path.write_text(
        f"previous run\n|  {old_url}  |\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(photon_tunnel, "state_dir", lambda: state_dir)
    monkeypatch.setattr(photon_tunnel, "state_path", lambda: state_path)
    monkeypatch.setattr(photon_tunnel, "log_path", lambda: log_path)
    monkeypatch.setattr(
        photon_tunnel,
        "resolve_cloudflared_binary",
        lambda **_kwargs: "/bin/cloudflared",
    )

    class FakeProc:
        pid = 12345
        returncode = None

        def poll(self) -> None:
            return None

    def fake_popen(*_args: Any, **kwargs: Any) -> FakeProc:
        stdout = kwargs["stdout"]
        stdout.write(f"|  {new_url}  |\n")
        stdout.flush()
        return FakeProc()

    monkeypatch.setattr(photon_tunnel.subprocess, "Popen", fake_popen)

    result = photon_tunnel.start(timeout_seconds=0.5, auto_install=False)

    assert result.success is True
    assert result.public_url == new_url
    assert result.webhook_url == f"{new_url}/photon/webhook"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["public_url"] == new_url


def test_start_force_new_stops_existing_tunnel(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    state_dir = tmp_path / "photon"
    state_dir.mkdir()
    state_path = state_dir / "tunnel.json"
    log_path = state_dir / "cloudflared.log"
    new_url = "https://new-one.trycloudflare.com"
    status_calls = [
        {"running": True, "pid": 111, "public_url": "https://old.trycloudflare.com"},
        {"running": False, "pid": None, "public_url": ""},
        {"running": False, "pid": None, "public_url": ""},
    ]
    stop_calls = []

    monkeypatch.setattr(photon_tunnel, "state_dir", lambda: state_dir)
    monkeypatch.setattr(photon_tunnel, "state_path", lambda: state_path)
    monkeypatch.setattr(photon_tunnel, "log_path", lambda: log_path)
    monkeypatch.setattr(
        photon_tunnel,
        "status",
        lambda: status_calls.pop(0) if status_calls else {"running": False},
    )
    monkeypatch.setattr(
        photon_tunnel,
        "stop",
        lambda: stop_calls.append(True) or {"stopped": True, "message": "stopped"},
    )
    monkeypatch.setattr(
        photon_tunnel,
        "resolve_cloudflared_binary",
        lambda **_kwargs: "/bin/cloudflared",
    )

    class FakeProc:
        pid = 222
        returncode = None

        def poll(self) -> None:
            return None

    def fake_popen(*_args: Any, **kwargs: Any) -> FakeProc:
        stdout = kwargs["stdout"]
        stdout.write(f"|  {new_url}  |\n")
        stdout.flush()
        return FakeProc()

    monkeypatch.setattr(photon_tunnel.subprocess, "Popen", fake_popen)

    result = photon_tunnel.start(
        timeout_seconds=0.5,
        auto_install=False,
        force_new=True,
    )

    assert stop_calls == [True]
    assert result.success is True
    assert result.reused is False
    assert result.public_url == new_url


def test_public_health_uses_explicit_dns_fallback(
    monkeypatch: Any,
) -> None:
    url = "https://fresh.trycloudflare.com/photon/webhook"

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError(
            socket.gaierror(8, "nodename nor servname provided, or not known")
        )

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        tool = Path(command[0]).name
        if tool == "dig":
            return photon_tunnel.subprocess.CompletedProcess(
                command,
                0,
                stdout="104.16.230.132\n",
                stderr="",
            )
        if tool == "curl":
            assert "--resolve" in command
            assert "fresh.trycloudflare.com:443:104.16.230.132" in command
            return photon_tunnel.subprocess.CompletedProcess(
                command,
                0,
                stdout="ok",
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(photon_tunnel.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(photon_tunnel.shutil, "which", fake_which)
    monkeypatch.setattr(photon_tunnel.subprocess, "run", fake_run)

    ok, detail = photon_tunnel.check_public_health(url)

    assert ok is True
    assert "explicit DNS fallback via 104.16.230.132" in detail


def test_public_health_classifies_system_dns_failure(
    monkeypatch: Any,
) -> None:
    url = "https://fresh.trycloudflare.com/photon/webhook"

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError(
            socket.gaierror(8, "nodename nor servname provided, or not known")
        )

    monkeypatch.setattr(photon_tunnel.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        photon_tunnel,
        "_check_public_health_with_explicit_dns",
        lambda *_args, **_kwargs: (False, ""),
    )

    ok, detail = photon_tunnel.check_public_health(url)

    assert ok is False
    assert "nodename nor servname provided" in detail
    assert "system DNS failed" in detail
    assert "resolve fresh.trycloudflare.com" in detail


def test_public_health_classifies_curl_style_dns_failure(
    monkeypatch: Any,
) -> None:
    def fake_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError("could not resolve host")

    monkeypatch.setattr(photon_tunnel.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        photon_tunnel,
        "_check_public_health_with_explicit_dns",
        lambda *_args, **_kwargs: (False, ""),
    )

    ok, detail = photon_tunnel.check_public_health(
        "https://fresh.trycloudflare.com/photon/webhook"
    )

    assert ok is False
    assert "could not resolve host" in detail
    assert "system DNS failed" in detail
    assert "resolve fresh.trycloudflare.com" in detail
