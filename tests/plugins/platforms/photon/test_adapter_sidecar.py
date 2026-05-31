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
    )

    assert env["HERMES_HOME"] == str(tmp_path)
    assert env["PHOTON_PROJECT_ID"] == "project-id"
    assert env["PHOTON_PROJECT_SECRET"] == "project-secret"
    assert env["PATH"] == "/usr/bin"
    # The stdio transport carries no loopback port / token any more.
    assert "PHOTON_SIDECAR_PORT" not in env
    assert "PHOTON_SIDECAR_TOKEN" not in env
