"""Group-chat mention gating: parity with the BlueBubbles iMessage channel.

With ``require_mention`` on, a group message is dropped unless it hits a wake-word pattern, and the
leading wake word is stripped from the ones that pass. Direct conversations are never gated. A
shared Beta line only serves direct spaces, so this matters for dedicated lines.
"""
from __future__ import annotations

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter
from tests.plugins.platforms.photon.conftest import FakePhotonApi

P = f"/v1/projects/{FakePhotonApi.PROJECT_ID}"
ATTACHMENT_ID = "pho_att_00000000000000000000000001"


def _adapter(extra: dict | None = None) -> PhotonAdapter:
    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra=extra or {}))
    adapter._api = photon_adapter._PhotonApi(FakePhotonApi.PROJECT_ID, FakePhotonApi.API_KEY)
    return adapter


def _capture(adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch) -> list[MessageEvent]:
    captured: list[MessageEvent] = []

    async def fake_handle(event: MessageEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


def _text(text: str, *, kind: str = "group", message_id: str = FakePhotonApi.MESSAGE_ID) -> dict:
    return FakePhotonApi.event(FakePhotonApi.message(
        {"type": "text", "format": "plain", "text": text}, kind=kind, message_id=message_id))


@pytest.fixture
def api(photon_async_api: FakePhotonApi, monkeypatch: pytest.MonkeyPatch) -> FakePhotonApi:
    monkeypatch.setenv("PHOTON_READ_RECEIPTS", "false")
    return photon_async_api


@pytest.mark.asyncio
async def test_group_message_dropped_without_mention(api, monkeypatch):
    adapter = _adapter({"require_mention": True})
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(_text("just chatting, no wake word"))

    assert captured == []


@pytest.mark.asyncio
async def test_group_message_with_mention_passes_without_the_wake_word(api, monkeypatch):
    adapter = _adapter({"require_mention": True})
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(_text("hermes, what's the weather"))

    [event] = captured
    assert event.text == "what's the weather"
    assert event.source.chat_type == "group"


@pytest.mark.asyncio
async def test_group_message_passes_when_gating_is_off(api, monkeypatch):
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(_text("hermes, what's the weather"))

    assert captured[0].text == "hermes, what's the weather"  # only a gated message is cleaned


@pytest.mark.asyncio
async def test_dm_never_gated(api, monkeypatch):
    adapter = _adapter({"require_mention": True})
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(_text("no wake word here", kind="direct"))

    [event] = captured
    assert (event.text, event.source.chat_type) == ("no wake word here", "dm")


def test_custom_mention_patterns_from_config(api):
    adapter = _adapter({"require_mention": True, "mention_patterns": [r"(?<![\w@])@?amos\b[,:\-]?"]})

    assert adapter.require_mention is True
    assert len(adapter._mention_patterns) == 1
    assert adapter._message_matches_mention_patterns("amos help me") is True
    assert adapter._message_matches_mention_patterns("hermes help me") is False


def test_mention_settings_from_env(api, monkeypatch):
    monkeypatch.setenv("PHOTON_REQUIRE_MENTION", "true")
    monkeypatch.setenv("PHOTON_MENTION_PATTERNS", r"bot\b, assistant\b")

    adapter = _adapter()

    assert adapter.require_mention is True
    assert len(adapter._mention_patterns) == 2
    assert adapter._message_matches_mention_patterns("hey bot") is True


def test_invalid_pattern_skipped(api):
    adapter = _adapter({"require_mention": True, "mention_patterns": ["(unclosed", r"good\b"]})

    assert len(adapter._mention_patterns) == 1
    assert adapter._message_matches_mention_patterns("a good thing") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("caption, downloads, dispatched", [
    ("holiday pic", 0, 0),
    ("hermes holiday pic", 1, 1),
], ids=["unmentioned", "mentioned"])
async def test_the_gate_runs_before_attachments_are_downloaded(api, monkeypatch, caption, downloads, dispatched):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}", api.attachment(ATTACHMENT_ID, size=len(png)))
    api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}/content",
           lambda request: httpx.Response(200, content=png, headers={"content-type": "image/png"}))
    adapter = _adapter({"require_mention": True})
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "group", "parts": [
        {"type": "text", "format": "plain", "text": caption},
        {"type": "attachment", "attachmentId": ATTACHMENT_ID}]}, kind="group")))

    assert len(api.calls("GET", f"{P}/attachments/{ATTACHMENT_ID}")) == downloads
    assert len(captured) == dispatched
