"""Inbound SDK-stream tests for the Photon adapter."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from plugins.platforms.photon import adapter as photon_adapter


def _make_adapter(monkeypatch: Any, tmp_path: Path) -> photon_adapter.PhotonAdapter:
    monkeypatch.delenv("PHOTON_PROJECT_ID", raising=False)
    monkeypatch.delenv("PHOTON_PROJECT_SECRET", raising=False)
    monkeypatch.delenv("PHOTON_OPERATOR_PHONE", raising=False)
    monkeypatch.setattr(photon_adapter, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        photon_adapter,
        "load_project_credentials",
        lambda: ("project-id", "project-secret"),
    )
    return photon_adapter.PhotonAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "project_name": "hermes-agent",
                "operator_phone": "+15550001000",
            },
        )
    )


def _sdk_text_event(message_id: str = "msg-1") -> dict[str, Any]:
    return {
        "id": message_id,
        "timestamp": "2026-05-30T12:00:00Z",
        "space": {"id": "any;-;+15550001000", "name": "Operator"},
        "sender": {"id": "+15550001000", "name": "Operator"},
        "content": {"type": "text", "text": "hello Hermes"},
    }


def _sdk_attachment_event(
    message_id: str,
    local_path: str,
    *,
    name: str = "photo.jpeg",
    mime_type: str = "image/jpeg",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "timestamp": "2026-05-30T12:00:00Z",
        "space": {"id": "any;-;+15550001000", "name": "Operator"},
        "sender": {"id": "+15550001000", "name": "Operator"},
        "content": {
            "type": "attachment",
            "name": name,
            "mimeType": mime_type,
            "localPath": local_path,
        },
    }


def test_sidecar_event_dispatches_message_event(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_text_event("msg-1")}
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event.text == "hello Hermes"
    assert event.message_type is MessageType.TEXT
    assert event.message_id == "msg-1"
    assert event.source.platform.value == "photon"
    assert event.source.chat_id == "any;-;+15550001000"
    assert event.source.chat_type == "dm"
    assert event.source.user_id == "+15550001000"


def test_sidecar_event_dedupes_repeated_message_id(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)
    sidecar_message = {"type": "event", "event": _sdk_text_event("msg-duplicate")}

    asyncio.run(adapter._handle_sidecar_message(sidecar_message))
    asyncio.run(adapter._handle_sidecar_message(sidecar_message))

    assert [event.message_id for event in events] == ["msg-duplicate"]


def test_sidecar_event_attachment_becomes_metadata_marker(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    "id": "msg-attachment",
                    "space": {"id": "any;-;+15550001000"},
                    "sender": {"id": "+15550001000"},
                    "content": {
                        "type": "attachment",
                        "name": "photo.heic",
                        "mimeType": "image/heic",
                    },
                },
            }
        )
    )

    assert len(events) == 1
    assert "Photon attachment received" in events[0].text
    assert "photo.heic" in events[0].text
    assert events[0].message_type is MessageType.PHOTO
    assert events[0].media_urls == []


def test_sidecar_event_group_attachment_items_are_normalized(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpeg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    "id": "msg-group",
                    "space": {"id": "any;-;+15550001000"},
                    "sender": {"id": "+15550001000"},
                    "content": {
                        "type": "group",
                        "items": [
                            {
                                "type": "attachment",
                                "name": "first.png",
                                "mimeType": "image/png",
                                "localPath": str(first),
                            },
                            {
                                "type": "attachment",
                                "name": "second.jpeg",
                                "mimeType": "image/jpeg",
                                "localPath": str(second),
                            },
                        ],
                    },
                },
            }
        )
    )

    assert len(events) == 1
    assert events[0].message_type is MessageType.PHOTO
    assert events[0].media_urls == [str(first), str(second)]
    assert events[0].media_types == ["image/png", "image/jpeg"]
    assert "Photon content type not handled: group" not in events[0].text
    assert "first.png" in events[0].text
    assert "second.jpeg" in events[0].text


def test_sidecar_event_attachment_includes_cached_media_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0
    image_path = tmp_path / "photo.heic"
    image_path.write_bytes(b"heic")
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    "id": "msg-cached-attachment",
                    "space": {"id": "any;-;+15550001000"},
                    "sender": {"id": "+15550001000"},
                    "content": {
                        "type": "attachment",
                        "name": "photo.heic",
                        "mimeType": "image/heic",
                        "localPath": str(image_path),
                    },
                },
            }
        )
    )

    assert len(events) == 1
    assert events[0].message_type is MessageType.PHOTO
    assert events[0].media_urls == [str(image_path)]
    assert events[0].media_types == ["image/heic"]
    assert str(image_path) in events[0].text


def test_sidecar_event_voice_attachment_uses_voice_type(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0
    audio_path = tmp_path / "voice.m4a"
    audio_path.write_bytes(b"audio")
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    "id": "msg-voice",
                    "space": {"id": "any;-;+15550001000"},
                    "sender": {"id": "+15550001000"},
                    "content": {
                        "type": "voice",
                        "name": "voice.m4a",
                        "mimeType": "audio/mp4",
                        "localPath": str(audio_path),
                    },
                },
            }
        )
    )

    assert len(events) == 1
    assert events[0].message_type is MessageType.VOICE
    assert events[0].media_urls == [str(audio_path)]
    assert events[0].media_types == ["audio/mp4"]


def test_malformed_sidecar_event_is_ignored(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    "id": "missing-space",
                    "sender": {"id": "+15550001000"},
                    "content": {"type": "text", "text": "ignored"},
                },
            }
        )
    )

    assert events == []


def test_sidecar_event_updates_runtime_state(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)

    async def capture(_event: MessageEvent) -> None:
        return None

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_text_event("msg-state")}
        )
    )

    state = photon_adapter.read_adapter_runtime_state()
    assert state["health"]["last_event_at"]
    assert state["health"]["project_id"] == "project-id"


def test_sidecar_batches_rapid_image_events(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0.01
    first = tmp_path / "one.jpeg"
    second = tmp_path / "two.jpeg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    async def run() -> None:
        await adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_attachment_event("msg-img-1", str(first))}
        )
        await adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_attachment_event("msg-img-2", str(second))}
        )
        assert events == []
        keys = list(adapter._pending_media_batches)
        assert len(keys) == 1
        await adapter._flush_media_batch_now(keys[0])
        adapter._cancel_media_batch_tasks()

    asyncio.run(run())

    assert len(events) == 1
    assert events[0].message_type is MessageType.PHOTO
    assert events[0].media_urls == [str(first), str(second)]
    assert events[0].media_types == ["image/jpeg", "image/jpeg"]


def test_sidecar_batches_text_caption_after_image_event(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0.01
    image_path = tmp_path / "captioned.jpeg"
    image_path.write_bytes(b"image")
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    async def run() -> None:
        await adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_attachment_event("msg-caption-img", str(image_path))}
        )
        await adapter._handle_sidecar_message(
            {
                "type": "event",
                "event": {
                    **_sdk_text_event("msg-caption-text"),
                    "content": {"type": "text", "text": "set the first button"},
                },
            }
        )
        assert events == []
        await asyncio.sleep(adapter._media_batch_delay_seconds + 0.03)

    asyncio.run(run())

    assert len(events) == 1
    assert events[0].message_type is MessageType.PHOTO
    assert events[0].media_urls == [str(image_path)]
    assert "captioned.jpeg" in events[0].text
    assert "set the first button" in events[0].text


def test_active_media_followup_queues_before_busy_handler(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    image_path = tmp_path / "followup.png"
    image_path.write_bytes(b"image")
    source = adapter.build_source(
        chat_id="any;-;+15550001000",
        chat_type="dm",
        user_id="+15550001000",
    )
    event = MessageEvent(
        text="[Photon attachment received: followup.png (image/png)]",
        message_type=MessageType.PHOTO,
        source=source,
        message_id="msg-active-media",
        media_urls=[str(image_path)],
        media_types=["image/png"],
    )
    session_key = adapter._media_batch_session_key(event)
    adapter.set_message_handler(AsyncMock(return_value="should not run"))
    busy_handler = AsyncMock(return_value=True)
    adapter.set_busy_session_handler(busy_handler)

    async def run() -> None:
        adapter._active_sessions[session_key] = asyncio.Event()
        await adapter.handle_message(event)

    asyncio.run(run())

    assert adapter._pending_messages[session_key] is event
    busy_handler.assert_not_called()
    adapter._message_handler.assert_not_called()


def test_active_plain_text_still_uses_busy_handler(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    source = adapter.build_source(
        chat_id="any;-;+15550001000",
        chat_type="dm",
        user_id="+15550001000",
    )
    event = MessageEvent(
        text="interrupt this",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-active-text",
    )
    session_key = adapter._media_batch_session_key(event)
    adapter.set_message_handler(AsyncMock(return_value="should not run"))
    busy_handler = AsyncMock(return_value=True)
    adapter.set_busy_session_handler(busy_handler)

    async def run() -> None:
        adapter._active_sessions[session_key] = asyncio.Event()
        await adapter.handle_message(event)

    asyncio.run(run())

    assert adapter._pending_messages == {}
    busy_handler.assert_awaited_once_with(event, session_key)
    adapter._message_handler.assert_not_called()


def test_sidecar_dispatches_plain_text_immediately_without_pending_media(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    adapter = _make_adapter(monkeypatch, tmp_path)
    adapter._media_batch_delay_seconds = 0.5
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)

    asyncio.run(
        adapter._handle_sidecar_message(
            {"type": "event", "event": _sdk_text_event("msg-immediate-text")}
        )
    )

    assert len(events) == 1
    assert events[0].text == "hello Hermes"
