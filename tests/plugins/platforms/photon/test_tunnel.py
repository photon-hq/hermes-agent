"""Tests for Photon managed tunnel helpers."""
from __future__ import annotations

import json
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
