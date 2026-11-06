"""The adapter's connection lifecycle: key probe, Socket.IO subscription, cursor and reconnects.

The Socket.IO client is replaced by a scripted stand-in; REST calls (the key probe) go to the
in-process fake API.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon import auth as photon_auth
from plugins.platforms.photon.adapter import PhotonAdapter
from tests.plugins.platforms.photon.conftest import FakePhotonApi
from tests.waiting import async_wait_until

PROJECT = f"/v1/projects/{FakePhotonApi.PROJECT_ID}"


class FakeSocket:
    """Stands in for ``socketio.AsyncClient``: records the connect and the calls, answers ``subscribe``
    with the factory's scripted ack."""

    def __init__(self, factory: Sockets, **kwargs: Any) -> None:
        self.factory, self.kwargs = factory, kwargs
        self.handlers: dict[str, Any] = {}
        self.url: str | None = None
        self.connect_kwargs: dict[str, Any] = {}
        self.calls: list[tuple[str, Any]] = []
        self.disconnected = False

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def connect(self, url: str, **kwargs: Any) -> None:
        if self.factory.connect_error is not None:
            raise self.factory.connect_error
        self.url, self.connect_kwargs = url, kwargs

    async def call(self, event: str, data: Any = None, timeout: float | None = None) -> Any:
        self.calls.append((event, data))
        return self.factory.ack

    async def disconnect(self) -> None:
        self.disconnected = True

    def deliver(self, event: dict, seq: int) -> None:
        self.handlers["delivery"](event, {"seq": seq})

    def drop(self) -> None:
        self.handlers["disconnect"]("transport close")


class Sockets:
    def __init__(self) -> None:
        self.made: list[FakeSocket] = []
        self.ack: Any = {"ok": True}
        self.connect_error: BaseException | None = None

    def __call__(self, **kwargs: Any) -> FakeSocket:
        self.made.append(FakeSocket(self, **kwargs))
        return self.made[-1]

    def subscribed(self) -> list[FakeSocket]:
        return [s for s in self.made if s.calls]


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> Sockets:
    factory = Sockets()
    monkeypatch.setattr(photon_adapter.socketio, "AsyncClient", factory)
    monkeypatch.setattr(photon_adapter.random, "uniform", lambda a, b: 0.0)  # reconnect without backoff
    return factory


@pytest_asyncio.fixture
async def adapter():
    made: list[PhotonAdapter] = []

    def make() -> PhotonAdapter:
        made.append(PhotonAdapter(PlatformConfig(enabled=True, extra={})))
        return made[-1]

    yield make
    for a in made:
        await a.disconnect()


def _capture(adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch) -> list[MessageEvent]:
    captured: list[MessageEvent] = []

    async def fake_handle(event: MessageEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


def _text(n: int) -> dict:
    message_id = f"pho_msg_{n:026d}"
    return FakePhotonApi.event(FakePhotonApi.message({"type": "text", "format": "plain", "text": f"m{n}"},
                                                     message_id=message_id))


@pytest.fixture
def api(photon_async_api: FakePhotonApi, monkeypatch: pytest.MonkeyPatch) -> FakePhotonApi:
    monkeypatch.setenv("PHOTON_READ_RECEIPTS", "false")
    photon_async_api.on("GET", PROJECT, FakePhotonApi.project())
    return photon_async_api


@pytest.mark.asyncio
async def test_connect_subscribes_from_the_saved_cursor(api, sockets, adapter):
    photon_adapter._save_cursor(FakePhotonApi.PROJECT_ID, 41)
    a = adapter()

    assert await a.connect() is True

    [socket] = sockets.made
    assert socket.kwargs["reconnection"] is False
    assert socket.url == photon_auth._api_host()
    assert socket.connect_kwargs["socketio_path"] == f"/v1/projects/{FakePhotonApi.PROJECT_ID}/events/ws"
    assert socket.connect_kwargs["headers"] == {"Authorization": f"Bearer {FakePhotonApi.API_KEY}"}
    assert socket.connect_kwargs["transports"] == ["websocket"]
    assert socket.calls == [("subscribe", {"startSeq": 41, "apiVersion": "2026-07-01"})]
    [probe] = api.calls("GET", PROJECT)
    assert probe.headers["authorization"] == f"Bearer {FakePhotonApi.API_KEY}"


@pytest.mark.asyncio
async def test_a_cursor_saved_for_another_project_starts_live(api, sockets, adapter):
    photon_adapter._save_cursor("pho_prj_00000000000000000000000009", 41)

    assert await adapter().connect() is True

    assert sockets.made[0].calls[0][1]["startSeq"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_rejected_key_is_fatal_before_the_socket_opens(api, sockets, adapter, status):
    api.on("GET", PROJECT, FakePhotonApi.problem(status, "UNAUTHORIZED"))
    a = adapter()

    assert await a.connect() is False

    assert a.fatal_error_code == "AUTH_REJECTED"
    assert a._fatal_error_retryable is False
    assert "hermes photon setup" in a.fatal_error_message
    assert sockets.made == []


@pytest.mark.asyncio
async def test_an_unreachable_api_is_retryable(api, sockets, adapter):
    _, body = FakePhotonApi.problem(503, "SERVICE_UNAVAILABLE")
    api.on("GET", PROJECT, lambda request: httpx.Response(503, json=body, headers={"retry-after": "0"}))
    a = adapter()

    assert await a.connect() is False

    assert (a.fatal_error_code, a._fatal_error_retryable) == ("CONNECT_FAILED", True)


@pytest.mark.asyncio
async def test_no_credentials_asks_for_setup(photon_home, sockets, adapter):
    a = adapter()

    assert await a.connect() is False

    assert (a.fatal_error_code, a._fatal_error_retryable) == ("MISSING_CREDENTIALS", False)


@pytest.mark.asyncio
async def test_spectrum_credentials_get_the_migration_notice(photon_home, sockets, adapter, monkeypatch):
    monkeypatch.setenv("PHOTON_PROJECT_ID", "3f1c2a9e-0000-4000-8000-000000000001")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "spectrum-secret")
    a = adapter()

    assert await a.connect() is False

    assert (a.fatal_error_code, a.fatal_error_message) == ("SPECTRUM_LEGACY", photon_auth.LEGACY_NOTICE)
    assert a._fatal_error_retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [True, False])
async def test_a_refused_subscription_fails_the_connect(api, sockets, adapter, retryable):
    sockets.ack = {"ok": False, "error": {"code": "invalid_cursor", "message": "no", "retryable": retryable}}
    a = adapter()

    assert await a.connect() is False

    assert (a.fatal_error_code, a._fatal_error_retryable) == ("STREAM_FAILED", retryable)
    assert sockets.made[0].disconnected


@pytest.mark.asyncio
async def test_deliveries_are_handled_in_order_and_checkpointed(api, sockets, adapter, monkeypatch):
    a = adapter()
    captured = _capture(a, monkeypatch)
    assert await a.connect()
    [socket] = sockets.made

    socket.deliver(_text(1), 1)
    socket.deliver(_text(2), 2)
    socket.deliver(_text(1), 1)  # at or before the cursor: already handled
    await async_wait_until(lambda: a._cursor == 2, what="cursor 2")

    assert [e.text for e in captured] == ["m1", "m2"]
    assert photon_adapter._load_cursor(FakePhotonApi.PROJECT_ID) == 2


@pytest.mark.asyncio
async def test_a_dropped_stream_resubscribes_from_the_last_handled_cursor(api, sockets, adapter, monkeypatch):
    a = adapter()
    captured = _capture(a, monkeypatch)
    assert await a.connect()
    first = sockets.made[0]
    first.deliver(_text(1), 1)
    await async_wait_until(lambda: a._cursor == 1, what="cursor 1")

    first.drop()
    first.deliver(_text(2), 2)  # buffered from the dead session: it replays after the resubscribe
    await async_wait_until(lambda: len(sockets.subscribed()) == 2, what="resubscribe")

    second = sockets.made[-1]
    assert second.calls == [("subscribe", {"startSeq": 1, "apiVersion": "2026-07-01"})]
    second.deliver(_text(2), 2)
    await async_wait_until(lambda: a._cursor == 2, what="cursor 2")
    assert [e.text for e in captured] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_a_reconnect_with_a_revoked_key_stops_for_good(api, sockets, adapter):
    a = adapter()
    assert await a.connect()
    api.on("GET", PROJECT, FakePhotonApi.problem(401, "UNAUTHORIZED"))
    sockets.connect_error = photon_adapter.socketio.exceptions.ConnectionError("Unexpected response from server")

    sockets.made[0].drop()
    await async_wait_until(lambda: a.fatal_error_code == "AUTH_REJECTED", what="fatal AUTH_REJECTED")

    assert a._stream_running is False
    assert a._fatal_error_retryable is False


@pytest.mark.asyncio
async def test_a_fatal_stream_error_stops_the_adapter(api, sockets, adapter):
    a = adapter()
    assert await a.connect()

    await a._on_delivery_error({"code": "lagging", "message": "slow consumer", "fatal": False})
    assert a._stream_running is True

    await a._on_delivery_error({"code": "project_deleted", "message": "gone", "fatal": True, "retryable": False})
    assert (a.fatal_error_code, a._stream_running) == ("STREAM_PROJECT_DELETED", False)


@pytest.mark.asyncio
async def test_disconnect_closes_the_socket_and_stops_reconnecting(api, sockets, adapter):
    a = adapter()
    assert await a.connect()
    socket = sockets.made[0]

    await a.disconnect()

    assert socket.disconnected
    assert (a._stream_task, a._worker_task, a._stream_running) == (None, None, False)
