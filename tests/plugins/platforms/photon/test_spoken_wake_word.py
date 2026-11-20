"""A spoken wake word admits a group voice note under ``require_mention`` (opt-in
``voice_mention_patterns``, #102330), on Photon 2.0's event shapes. Only the voice part is sent
to STT, and a voice note waiting on gate STT runs off the delivery worker, FIFO per chat: it
delays only its own chat, and a running-agent escape hatch (``/stop``) never waits behind it.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_inbound_transcription import GatewayInboundTranscriptionMixin
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter

from tests.plugins.platforms.photon.conftest import FakePhotonApi

P = f"/v1/projects/{FakePhotonApi.PROJECT_ID}"
GROUP = "pho_spc_00000000000000000000000002"
PATTERNS = [r"(?i)\bhermes\b"]
VOICE_ID = "pho_att_00000000000000000000000001"
FILE_ID = "pho_att_00000000000000000000000002"
OGG = b"OggS" + b"\x01" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
VOICE = {"type": "voice", "attachmentId": VOICE_ID}


@pytest.fixture
def home(photon_async_api, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PHOTON_READ_RECEIPTS", "false")
    return tmp_path


def _serve(api: FakePhotonApi, attachment_id: str, data: bytes, content_type: str, filename: str) -> None:
    api.on("GET", f"{P}/attachments/{attachment_id}",
           api.attachment(attachment_id, content_type=content_type, size=len(data), filename=filename))
    api.on("GET", f"{P}/attachments/{attachment_id}/content",
           lambda _request: httpx.Response(200, content=data, headers={"content-type": content_type}))


def _adapter(seen: list | None = None, *, voice_mention_patterns: bool = True) -> PhotonAdapter:
    adapter = PhotonAdapter(PlatformConfig(enabled=True, extra={
        "require_mention": True, "mention_patterns": PATTERNS, "voice_mention_patterns": voice_mention_patterns}))
    adapter._api = photon_adapter._PhotonApi(FakePhotonApi.PROJECT_ID, FakePhotonApi.API_KEY)
    adapter.handle_message = AsyncMock(side_effect=None if seen is None else lambda e: seen.append(e.message_id))
    return adapter


def _event(message_id: str, content: dict, *, space: str = GROUP, parent: str | None = None) -> dict:
    direct = space == FakePhotonApi.SPACE_ID
    message = FakePhotonApi.message(content, message_id=message_id, kind="direct" if direct else "group", parent=parent)
    message["space"]["id"] = space
    return FakePhotonApi.event(message)


def _text(text: str) -> dict:
    return {"type": "text", "format": "plain", "text": text}


def _stt(transcript: str):
    calls = []

    def transcribe(path, _model=None, _source=None):
        calls.append(path)
        return {"success": True, "transcript": transcript, "provider": "fake"}
    return transcribe, calls


def _slow_stt(release: threading.Event):
    def transcribe(_path, _model=None, _source=None):
        release.wait(10)
        return {"success": True, "transcript": "hermes, what is on today", "provider": "fake"}
    return transcribe


async def _drain(adapter: PhotonAdapter) -> None:
    await asyncio.gather(*adapter._background_tasks)


async def _dispatch(adapter: PhotonAdapter, *events: dict) -> None:
    for event in events:  # the delivery worker's loop: each returns before the next is read
        await asyncio.wait_for(adapter._dispatch_event(event), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken,admitted", [("hermes, what is on today", True), ("lunch?", False)])
async def test_a_group_voice_note_wakes_on_the_spoken_wake_word(photon_async_api, home, spoken, admitted):
    _serve(photon_async_api, VOICE_ID, OGG, "audio/ogg", "voice.ogg")
    adapter = _adapter()
    transcribe, calls = _stt(spoken)
    with patch("tools.transcription_tools.transcribe_audio", side_effect=transcribe):
        await _dispatch(adapter, _event("v1", VOICE))
        await _drain(adapter)

    assert len(calls) == 1
    assert adapter.handle_message.await_count == int(admitted)
    assert len(list(home.rglob("*.ogg"))) == int(admitted)  # a rejected note leaves no clip behind
    if admitted:
        event = adapter.handle_message.await_args.args[0]
        assert event.message_type == MessageType.VOICE and event.media_types == ["audio/ogg"]


@pytest.mark.asyncio
async def test_a_voice_note_is_never_fetched_without_the_opt_in(photon_async_api, home):
    adapter = _adapter(voice_mention_patterns=False)
    transcribe, calls = _stt("hermes, what is on today")
    with patch("tools.transcription_tools.transcribe_audio", side_effect=transcribe):
        await _dispatch(adapter, _event("v1", VOICE))
        await _drain(adapter)

    assert calls == [] and adapter.handle_message.await_count == 0
    assert not photon_async_api.calls("GET", f"{P}/attachments/{VOICE_ID}")


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken,admitted", [("hermes, look at this", True), ("just chatting about lunch", False)])
@pytest.mark.parametrize("first", [(PNG, "image/png", "p.png"), (b"%PDF-1.4", "application/pdf", "a.pdf")],
                         ids=["photo", "pdf"])
async def test_only_the_voice_part_of_a_payload_reaches_stt(photon_async_api, home, first, spoken, admitted):
    """A photo or PDF beside the voice note is never transcribed, a rejected payload leaves none of
    its downloads behind, and an admitted one is VOICE, so the turn's STT pass (and a busy
    session's) takes its audio and only its audio."""
    _serve(photon_async_api, FILE_ID, *first)
    _serve(photon_async_api, VOICE_ID, OGG, "audio/ogg", "voice.ogg")
    adapter = _adapter()
    transcribe, calls = _stt(spoken)
    content = {"type": "group", "parts": [{"type": "attachment", "attachmentId": FILE_ID}, VOICE]}
    with patch("tools.transcription_tools.transcribe_audio", side_effect=transcribe):
        await _dispatch(adapter, _event("mx1", content))
        await _drain(adapter)

    assert len(calls) == 1 and calls[0].endswith(".ogg")
    assert adapter.handle_message.await_count == int(admitted)
    cached = [p for p in home.rglob("*") if p.is_file() and p.suffix in (".png", ".pdf", ".ogg")]
    assert len(cached) == (2 if admitted else 0)
    if admitted:
        event = adapter.handle_message.await_args.args[0]
        assert event.message_type == MessageType.VOICE and len(event.media_urls) == 2
        _images, stt_paths, audio_files, _videos = GatewayInboundMixin._classify_inbound_media(event, False)
        assert len(stt_paths) == 1 and stt_paths[0].endswith(".ogg") and audio_files == []
        assert GatewayInboundTranscriptionMixin._pending_event_audio_paths(None, event) == stt_paths


@pytest.mark.asyncio
async def test_a_caf_voice_note_sent_as_octet_stream_is_transcribed_as_voice(photon_async_api, home):
    """iMessage voice notes are CAF and may come typed ``application/octet-stream``: cached as audio,
    so typed as audio too, or the turn would never send them to STT."""
    _serve(photon_async_api, VOICE_ID, b"caff" + b"\x00" * 60, "application/octet-stream", "Audio Message.caf")
    adapter = _adapter()
    transcribe, calls = _stt("hermes, what is on today")
    with patch("tools.transcription_tools.transcribe_audio", side_effect=transcribe):
        await _dispatch(adapter, _event("caf1", VOICE))
        await _drain(adapter)

    assert len(calls) == 1 and adapter.handle_message.await_count == 1
    event = adapter.handle_message.await_args.args[0]
    assert event.media_types == ["audio/x-caf"]
    _images, stt_paths, _audio_files, _videos = GatewayInboundMixin._classify_inbound_media(event, False)
    assert stt_paths == event.media_urls and len(stt_paths) == 1


@pytest.mark.asyncio
async def test_gate_stt_never_holds_the_delivery_worker(photon_async_api, home):
    """A slow group voice note delays only its own chat: a DM's ``/stop`` is dispatched meanwhile,
    and a later message in the same group still arrives after the note."""
    _serve(photon_async_api, VOICE_ID, OGG, "audio/ogg", "voice.ogg")
    seen: list[str] = []
    adapter = _adapter(seen)
    release = threading.Event()
    try:
        with patch("tools.transcription_tools.transcribe_audio", side_effect=_slow_stt(release)):
            await _dispatch(adapter, _event("v1", VOICE), _event("t1", _text("hermes later")),
                            _event("s1", _text("/stop"), space=FakePhotonApi.SPACE_ID))
            assert seen == ["s1"]
            release.set()
            await _drain(adapter)
    finally:
        release.set()
    assert seen == ["s1", "v1", "t1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [None, FakePhotonApi.MESSAGE_ID], ids=["message", "threaded-reply"])
async def test_a_group_stop_never_waits_on_an_earlier_voice_check(photon_async_api, home, parent):
    """A same-group ``/stop``, typed or as a threaded reply, is dispatched while an earlier voice note
    is at gate STT; ordinary text still waits behind the note."""
    _serve(photon_async_api, VOICE_ID, OGG, "audio/ogg", "voice.ogg")
    seen: list[str] = []
    adapter = _adapter(seen)
    release = threading.Event()
    try:
        with patch("tools.transcription_tools.transcribe_audio", side_effect=_slow_stt(release)):
            # Photon's group mention gate needs the wake word on a command too.
            await _dispatch(adapter, _event("v1", VOICE), _event("t1", _text("hermes later")),
                            _event("s1", _text("/stop hermes"), parent=parent))
            assert seen == ["s1"]
            release.set()
            await _drain(adapter)
    finally:
        release.set()
    assert seen == ["s1", "v1", "t1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new hermes", "/undo hermes", "/status hermes"])
async def test_a_session_command_keeps_its_place_behind_an_earlier_voice_note(photon_async_api, home, command):
    """Only the running-agent escape hatches skip the per-chat order: ``/new``, ``/undo`` and
    ``/status`` sent after a voice note still at gate STT run after that note."""
    _serve(photon_async_api, VOICE_ID, OGG, "audio/ogg", "voice.ogg")
    seen: list[str] = []
    adapter = _adapter(seen)
    release = threading.Event()
    try:
        with patch("tools.transcription_tools.transcribe_audio", side_effect=_slow_stt(release)):
            await _dispatch(adapter, _event("v1", VOICE), _event("c1", _text(command)),
                            _event("t1", _text("hermes later")))
            assert seen == []
            release.set()
            await _drain(adapter)
    finally:
        release.set()
    assert seen == ["v1", "c1", "t1"]
