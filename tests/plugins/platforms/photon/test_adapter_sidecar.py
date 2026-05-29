"""Tests for Photon adapter sidecar process setup."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from plugins.platforms.photon import adapter as photon_adapter


def test_sidecar_process_env_exports_canonical_hermes_home(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(photon_adapter, "get_hermes_home", lambda: tmp_path)

    env = photon_adapter._sidecar_process_env(
        project_id="project-id",
        project_secret="project-secret",
        sidecar_port=8789,
        sidecar_bind="127.0.0.1",
        sidecar_token="token",
    )

    assert env["HERMES_HOME"] == str(tmp_path)
    assert env["PHOTON_PROJECT_ID"] == "project-id"
    assert env["PHOTON_PROJECT_SECRET"] == "project-secret"
    assert env["PHOTON_SIDECAR_PORT"] == "8789"
    assert env["PHOTON_SIDECAR_BIND"] == "127.0.0.1"
    assert env["PHOTON_SIDECAR_TOKEN"] == "token"
    assert env["PATH"] == "/usr/bin"
