"""Tests for normalizing sidecar NDJSON ``message`` events into MessageEvents."""
from __future__ import annotations

from typing import Any

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.photon.adapter import PhotonAdapter


def _adapter() -> PhotonAdapter:
    return PhotonAdapter(
        PlatformConfig(extra={"project_id": "p", "project_secret": "s"})
    )


def test_dm_text_event_normalizes() -> None:
    adapter = _adapter()
    event = {
        "id": "m1",
        "spaceId": "any;-;+15551234567",
        "sender": "+15551234567",
        "content": {"type": "text", "text": "hi there"},
        "timestamp": "2026-05-30T00:00:00Z",
    }
    me = adapter._event_to_message_event(event)
    assert me is not None
    assert me.text == "hi there"
    assert me.message_type == MessageType.TEXT
    assert me.message_id == "m1"
    assert me.source.chat_type == "dm"
    assert me.source.chat_id == "any;-;+15551234567"


def test_group_event_uses_group_chat_type() -> None:
    adapter = _adapter()
    me = adapter._event_to_message_event(
        {
            "id": "m2",
            "spaceId": "any;+;some-chat-guid",
            "sender": "+15550000000",
            "content": {"type": "text", "text": "yo"},
        }
    )
    assert me is not None
    assert me.source.chat_type == "group"


def test_attachment_event_maps_mime_to_message_type() -> None:
    adapter = _adapter()
    me = adapter._event_to_message_event(
        {
            "id": "m3",
            "spaceId": "any;-;+15551234567",
            "content": {"type": "attachment", "name": "pic.jpg", "mimeType": "image/jpeg"},
        }
    )
    assert me is not None
    assert me.message_type == MessageType.PHOTO
    assert "pic.jpg" in me.text


def test_missing_space_id_is_dropped() -> None:
    adapter = _adapter()
    assert adapter._event_to_message_event({"id": "m4", "content": {"type": "text", "text": "x"}}) is None


def test_bad_timestamp_falls_back_without_raising() -> None:
    adapter = _adapter()
    me = adapter._event_to_message_event(
        {
            "id": "m5",
            "spaceId": "any;-;+15551234567",
            "content": {"type": "text", "text": "x"},
            "timestamp": "not-a-timestamp",
        }
    )
    assert me is not None
    assert me.timestamp is not None


def test_unknown_content_type_is_surfaced_as_text() -> None:
    adapter = _adapter()
    me = adapter._event_to_message_event(
        {
            "id": "m6",
            "spaceId": "any;-;+15551234567",
            "content": {"type": "poll"},
        }
    )
    assert me is not None
    assert me.message_type == MessageType.TEXT
    assert "poll" in me.text


def test_dedup_suppresses_repeat_ids() -> None:
    adapter = _adapter()
    assert adapter._is_duplicate("dup-1") is False
    assert adapter._is_duplicate("dup-1") is True
    assert adapter._is_duplicate("dup-2") is False


def test_resolve_pending_sets_future_result_and_error(monkeypatch: Any) -> None:
    import asyncio

    async def _run() -> None:
        adapter = _adapter()
        loop = asyncio.get_event_loop()

        ok_future = loop.create_future()
        adapter._pending["cid-ok"] = ok_future
        adapter._resolve_pending({"type": "sent", "cid": "cid-ok", "ok": True, "messageId": "x"})
        assert (await ok_future)["messageId"] == "x"

        err_future = loop.create_future()
        adapter._pending["cid-err"] = err_future
        adapter._resolve_pending({"type": "error", "cid": "cid-err", "ok": False, "error": "boom"})
        try:
            await err_future
            raise AssertionError("expected error")
        except RuntimeError as e:
            assert "boom" in str(e)

    asyncio.run(_run())
