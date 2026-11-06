"""Outbound Photon 2.0 sends: text, images, typing and the standalone (cron) sender.

Requests go through the real ``AsyncPhoton`` client into the in-process fake API, so each body is
validated by the SDK against the contract before it is asserted on here.
"""
from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter
from tests.plugins.platforms.photon.conftest import FakePhotonApi

P = f"/v1/projects/{FakePhotonApi.PROJECT_ID}"
MESSAGES = f"{P}/messages"
TYPING = f"{P}/spaces/{FakePhotonApi.SPACE_ID}/typing"
SENT_ID = "pho_msg_00000000000000000000000009"
ATTACHMENT_ID = "pho_att_00000000000000000000000001"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ROUTE = photon_adapter._Route(FakePhotonApi.SPACE_ID, FakePhotonApi.AGENT_ID)


def _sent(content: dict | None = None) -> tuple:
    return 202, FakePhotonApi.message(content or {"type": "text", "format": "plain", "text": "ok"},
                                      message_id=SENT_ID, status="dispatched")


def _adapter(*, known_route: bool = True) -> PhotonAdapter:
    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._api = photon_adapter._PhotonApi(FakePhotonApi.PROJECT_ID, FakePhotonApi.API_KEY)
    if known_route:
        adapter._api.remember(ROUTE, FakePhotonApi.PHONE)
    return adapter


def _sent_contents(api: FakePhotonApi) -> list[dict]:
    return [FakePhotonApi.body(r)["content"] for r in api.calls("POST", MESSAGES)]


def _retry_now(status: int, code: str):
    """A transient problem the SDK retries at once (Retry-After: 0) instead of backing off."""
    _, body = FakePhotonApi.problem(status, code)
    return lambda request: httpx.Response(status, json=body, headers={"retry-after": "0"})


@pytest.mark.asyncio
async def test_a_reply_goes_out_as_markdown_from_the_line(photon_async_api):
    photon_async_api.on("POST", MESSAGES, _sent())

    result = await _adapter().send(FakePhotonApi.SPACE_ID, "**done**: see https://example.com")

    assert (result.success, result.message_id) == (True, SENT_ID)
    [request] = photon_async_api.calls("POST", MESSAGES)
    assert FakePhotonApi.body(request) == {
        "spaceId": FakePhotonApi.SPACE_ID, "senderId": FakePhotonApi.AGENT_ID,
        "content": {"type": "text", "format": "markdown", "text": "**done**: see https://example.com"}}
    assert request.headers["authorization"] == f"Bearer {FakePhotonApi.API_KEY}"
    assert request.headers["idempotency-key"].startswith("hermes:")


@pytest.mark.asyncio
async def test_markdown_photon_refuses_is_resent_as_plain_text(photon_async_api):
    photon_async_api.on("POST", MESSAGES, FakePhotonApi.problem(422, "INVALID_CONTENT"), _sent())

    result = await _adapter().send(FakePhotonApi.SPACE_ID, "**done**")

    assert result.success
    assert _sent_contents(photon_async_api) == [
        {"type": "text", "format": "markdown", "text": "**done**"},
        {"type": "text", "format": "plain", "text": "done"}]


@pytest.mark.asyncio
async def test_markdown_can_be_turned_off(photon_async_api, monkeypatch):
    monkeypatch.setenv("PHOTON_MARKDOWN", "false")
    photon_async_api.on("POST", MESSAGES, _sent())

    assert (await _adapter().send(FakePhotonApi.SPACE_ID, "**done**")).success

    assert _sent_contents(photon_async_api) == [{"type": "text", "format": "plain", "text": "done"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, retryable", [
    (FakePhotonApi.problem(409, "SPACE_NOT_SENDABLE"), False),
    (FakePhotonApi.problem(422, "VALIDATION_FAILED"), False),
    (_retry_now(429, "RATE_LIMITED"), True),
    (_retry_now(503, "SERVICE_UNAVAILABLE"), True),
], ids=["not-sendable", "invalid", "rate-limited", "unavailable"])
async def test_a_refused_send_is_final_unless_it_is_transient(photon_async_api, reply, retryable):
    photon_async_api.on("POST", MESSAGES, reply)
    adapter = _adapter()

    result = await adapter.send(FakePhotonApi.SPACE_ID, "hello")

    assert result.success is False
    assert result.retryable is retryable
    assert adapter._send_retry_is_final(result) is not retryable


@pytest.mark.asyncio
async def test_a_phone_number_resolves_to_the_conversation_it_texted_from(photon_async_api):
    other = FakePhotonApi.message(handle="+15550000000", message_id="pho_msg_00000000000000000000000005")
    photon_async_api.on("GET", MESSAGES, {"items": [other], "nextPageToken": "page.two"},
                        {"items": [FakePhotonApi.message()]})
    photon_async_api.on("POST", MESSAGES, _sent())
    adapter = _adapter(known_route=False)

    assert (await adapter.send("+1 (555) 123-4567", "hello")).success

    first, second = photon_async_api.calls("GET", MESSAGES)
    assert dict(first.url.params) == {"order": "desc", "platform": "imessage", "direction": "received", "pageSize": "100"}
    assert second.url.params["pageToken"] == "page.two"
    [send] = photon_async_api.calls("POST", MESSAGES)
    assert FakePhotonApi.body(send)["spaceId"] == FakePhotonApi.SPACE_ID
    assert FakePhotonApi.body(send)["senderId"] == FakePhotonApi.AGENT_ID


@pytest.mark.asyncio
async def test_a_space_id_resolves_from_its_latest_message(photon_async_api):
    photon_async_api.on("GET", MESSAGES, {"items": [FakePhotonApi.message(status="dispatched")]})
    photon_async_api.on("POST", MESSAGES, _sent())

    assert (await _adapter(known_route=False).send(FakePhotonApi.SPACE_ID, "hello")).success

    [lookup] = photon_async_api.calls("GET", MESSAGES)
    assert lookup.url.params["spaceId"] == FakePhotonApi.SPACE_ID
    assert FakePhotonApi.body(photon_async_api.calls("POST", MESSAGES)[0])["senderId"] == FakePhotonApi.AGENT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["+15559999999", "not-a-target"])
async def test_a_target_the_line_cannot_reach_is_a_final_failure(photon_async_api, target):
    photon_async_api.on("GET", MESSAGES, {"items": []})
    adapter = _adapter(known_route=False)

    result = await adapter.send(target, "hello")

    assert result.success is False
    assert adapter._send_retry_is_final(result)
    assert not photon_async_api.calls("POST", MESSAGES)


@pytest.mark.asyncio
async def test_an_image_is_uploaded_then_sent_with_its_caption(photon_async_api, tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(PNG)
    photon_async_api.on("POST", f"{P}/attachments", (201, photon_async_api.attachment(
        ATTACHMENT_ID, size=len(PNG), filename="chart.png")))
    photon_async_api.on("POST", MESSAGES, _sent())

    result = await _adapter().send_image_file(FakePhotonApi.SPACE_ID, str(image), caption="**Q3** revenue")

    assert (result.success, result.message_id) == (True, SENT_ID)
    [upload] = photon_async_api.calls("POST", f"{P}/attachments")
    assert upload.headers["content-length"] == str(len(upload.content))
    assert upload.headers["idempotency-key"].startswith("hermes:")
    parsed = BytesParser(policy=HTTP).parsebytes(
        b"content-type: " + upload.headers["content-type"].encode() + b"\r\n\r\n" + upload.content)
    assert parsed.get_content_type() == "multipart/related"
    description, data = parsed.get_payload()
    assert json.loads(description.get_payload()) == {"contentType": "image/png", "sizeBytes": len(PNG), "filename": "chart.png"}
    assert (data.get_content_type(), data.get_payload(decode=True)) == ("image/png", PNG)
    assert _sent_contents(photon_async_api) == [{"type": "attachment", "attachmentId": ATTACHMENT_ID,
                                                  "metadata": {"caption": {"format": "plain", "text": "Q3 revenue"}}}]


@pytest.mark.asyncio
async def test_a_missing_image_file_is_refused_before_any_request(photon_async_api, tmp_path):
    result = await _adapter().send_image_file(FakePhotonApi.SPACE_ID, str(tmp_path / "gone.png"))

    assert result.success is False
    assert photon_async_api.requests == []


@pytest.mark.asyncio
async def test_typing_is_rate_limited_per_chat_and_cleared_on_stop(photon_async_api):
    photon_async_api.on("POST", TYPING, (202, FakePhotonApi.message({"type": "space.typing.update", "typing": True},
                                                                     status="dispatched")))
    adapter = _adapter()

    await adapter.send_typing(FakePhotonApi.SPACE_ID)
    await adapter.send_typing(FakePhotonApi.SPACE_ID)
    await adapter.stop_typing(FakePhotonApi.SPACE_ID)

    assert [FakePhotonApi.body(r) for r in photon_async_api.calls("POST", TYPING)] == [
        {"senderId": FakePhotonApi.AGENT_ID, "typing": True}, {"senderId": FakePhotonApi.AGENT_ID, "typing": False}]


@pytest.mark.asyncio
async def test_the_standalone_sender_texts_then_sends_images_only(photon_async_api, tmp_path):
    image, document = tmp_path / "chart.png", tmp_path / "report.pdf"
    image.write_bytes(PNG)
    document.write_bytes(b"%PDF-1.4")
    photon_async_api.on("GET", MESSAGES, {"items": [FakePhotonApi.message()]})
    photon_async_api.on("POST", f"{P}/attachments", (201, photon_async_api.attachment(ATTACHMENT_ID, size=len(PNG))))
    photon_async_api.on("POST", MESSAGES, _sent())

    result = await photon_adapter._standalone_send(
        PlatformConfig(enabled=True, extra={}), FakePhotonApi.PHONE, "daily report",
        media_files=[(str(image), False), (str(document), False)])

    assert result == {"success": True, "message_id": SENT_ID}
    assert [c["type"] for c in _sent_contents(photon_async_api)] == ["text", "attachment"]
    assert len(photon_async_api.calls("POST", f"{P}/attachments")) == 1


@pytest.mark.asyncio
async def test_the_standalone_sender_needs_credentials(photon_home):
    result = await photon_adapter._standalone_send(PlatformConfig(enabled=True, extra={}), FakePhotonApi.PHONE, "hi")

    assert "PHOTON_PROJECT_ID and PHOTON_API_KEY are required" in result["error"]
