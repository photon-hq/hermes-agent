"""Inbound Photon 2.0 events: what a socket delivery turns into on the gateway side.

These drive ``_dispatch_event`` directly with the public event envelope; REST calls the
handler makes (attachment downloads, read receipts) go to the in-process fake API.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter

from tests.plugins.platforms.photon.conftest import FakePhotonApi

P = f"/v1/projects/{FakePhotonApi.PROJECT_ID}"
READ = f"{P}/messages/{FakePhotonApi.MESSAGE_ID}/read"
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


def _read_ack(api: FakePhotonApi) -> None:
    api.on("POST", READ, (202, api.message({"type": "message.read"}, status="dispatched")))


@pytest.mark.asyncio
async def test_direct_text_reaches_the_gateway_from_the_senders_number(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "text", "format": "plain", "text": "hello"})))

    [event] = captured
    assert (event.text, event.message_type, event.message_id) == ("hello", MessageType.TEXT, FakePhotonApi.MESSAGE_ID)
    assert event.source.chat_id == FakePhotonApi.SPACE_ID
    assert event.source.chat_type == "dm"
    assert event.source.user_id == FakePhotonApi.PHONE  # PHOTON_ALLOWED_USERS holds phone numbers
    [read] = photon_async_api.calls("POST", READ)
    assert read.headers["idempotency-key"] == f"hermes-read:{FakePhotonApi.MESSAGE_ID}"


@pytest.mark.asyncio
async def test_the_conversation_becomes_a_send_route(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    adapter = _adapter()
    _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message()))

    expected = photon_adapter._Route(FakePhotonApi.SPACE_ID, FakePhotonApi.AGENT_ID)
    assert await adapter._api.route(FakePhotonApi.SPACE_ID) == expected
    assert await adapter._api.route(FakePhotonApi.PHONE) == expected
    assert not photon_async_api.calls("GET", f"{P}/messages")  # both came from the event, not a lookup


@pytest.mark.asyncio
async def test_a_group_message_never_becomes_its_senders_phone_route(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    photon_async_api.on("GET", f"{P}/messages", {"items": []})
    adapter = _adapter()
    _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message(kind="group")))

    expected = photon_adapter._Route(FakePhotonApi.SPACE_ID, FakePhotonApi.AGENT_ID)
    assert await adapter._api.route(FakePhotonApi.SPACE_ID) == expected
    with pytest.raises(LookupError):
        await adapter._api.route(FakePhotonApi.PHONE)


@pytest.mark.asyncio
async def test_read_receipts_can_be_turned_off(photon_async_api, monkeypatch):
    monkeypatch.setenv("PHOTON_READ_RECEIPTS", "false")
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message()))

    assert len(captured) == 1
    assert not photon_async_api.calls("POST", READ)


@pytest.mark.asyncio
async def test_a_replayed_message_is_handled_once(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)
    event = FakePhotonApi.event(FakePhotonApi.message())

    await adapter._dispatch_event(event)
    await adapter._dispatch_event(event)

    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    FakePhotonApi.event(FakePhotonApi.message(), event_type="message.delivered"),
    FakePhotonApi.event({**FakePhotonApi.message(), "platform": "sms"}),
    FakePhotonApi.event(FakePhotonApi.message({"type": "space.typing.update", "typing": True})),
    FakePhotonApi.event(FakePhotonApi.message({"type": "message.read"})),
], ids=["not-a-received-message", "other-platform", "typing", "read"])
async def test_events_that_are_not_a_users_message_are_ignored(photon_async_api, monkeypatch, event):
    _read_ack(photon_async_api)
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(event)

    assert captured == []


@pytest.mark.asyncio
async def test_an_image_is_downloaded_into_the_media_cache(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}", photon_async_api.attachment(ATTACHMENT_ID, size=len(png)))
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}/content",
                        lambda request: httpx.Response(200, content=png, headers={"content-type": "image/png"}))
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "attachment", "attachmentId": ATTACHMENT_ID})))

    [event] = captured
    assert (event.text, event.message_type, event.media_types) == ("(attachment)", MessageType.PHOTO, ["image/png"])
    assert Path(event.media_urls[0]).read_bytes() == png


@pytest.mark.asyncio
async def test_an_oversized_attachment_becomes_a_marker_without_downloading(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}", photon_async_api.attachment(
        ATTACHMENT_ID, content_type="video/mp4", filename="clip.mp4", size=photon_adapter._MAX_INBOUND_ATTACHMENT_BYTES + 1))
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "attachment", "attachmentId": ATTACHMENT_ID})))

    [event] = captured
    assert event.text == "[Photon attachment received: clip.mp4 (video/mp4)]"
    assert event.media_urls == []
    assert not photon_async_api.calls("GET", f"{P}/attachments/{ATTACHMENT_ID}/content")


@pytest.mark.asyncio
async def test_a_failed_download_still_tells_the_agent_something_arrived(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}", FakePhotonApi.problem(403, "FORBIDDEN"))
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "voice", "attachmentId": ATTACHMENT_ID})))

    [event] = captured
    assert event.message_type == MessageType.VOICE
    assert event.text == f"[Photon voice received: {ATTACHMENT_ID} (unknown MIME)]"


@pytest.mark.asyncio
async def test_a_group_payload_is_one_event_with_its_text_and_media(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}", photon_async_api.attachment(ATTACHMENT_ID, size=len(png)))
    photon_async_api.on("GET", f"{P}/attachments/{ATTACHMENT_ID}/content",
                        lambda request: httpx.Response(200, content=png, headers={"content-type": "image/png"}))
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "group", "parts": [
        {"type": "text", "format": "plain", "text": "look at this"},
        {"type": "attachment", "attachmentId": ATTACHMENT_ID}]})))

    [event] = captured
    assert (event.text, event.message_type, event.media_types) == ("look at this", MessageType.PHOTO, ["image/png"])
    assert len(event.media_urls) == 1


@pytest.mark.asyncio
async def test_unknown_content_reaches_the_agent_as_a_marker(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message({"type": "poll", "title": "lunch?"})))

    assert captured[0].text == "[Photon content type not handled: poll]"


@pytest.mark.asyncio
async def test_a_threaded_reply_to_our_message_carries_the_quoted_text(photon_async_api, monkeypatch):
    sent_id = "pho_msg_00000000000000000000000009"
    photon_async_api.on("POST", f"{P}/messages", (202, FakePhotonApi.message(
        {"type": "text", "format": "markdown", "text": "the answer is 42"}, message_id=sent_id, status="dispatched")))
    _read_ack(photon_async_api)
    adapter = _adapter()
    adapter._api.remember(photon_adapter._Route(FakePhotonApi.SPACE_ID, FakePhotonApi.AGENT_ID))
    captured = _capture(adapter, monkeypatch)
    assert (await adapter.send(FakePhotonApi.SPACE_ID, "the answer is 42")).message_id == sent_id

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message(
        {"type": "text", "format": "plain", "text": "why?"}, parent=sent_id)))

    [event] = captured
    assert event.reply_to_message_id == sent_id
    assert event.reply_to_text == "the answer is 42"
    assert event.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_a_reply_to_the_users_own_message_is_not_marked_ours(photon_async_api, monkeypatch):
    _read_ack(photon_async_api)
    adapter = _adapter()
    captured = _capture(adapter, monkeypatch)
    users_message = "pho_msg_00000000000000000000000007"

    await adapter._dispatch_event(FakePhotonApi.event(FakePhotonApi.message(parent=users_message)))

    [event] = captured
    assert (event.reply_to_message_id, event.reply_to_text, event.reply_to_is_own_message) == (users_message, None, False)
