"""Photon 2.0 (iMessage) platform adapter.

Inbound: the project's Socket.IO event stream (``/v1/projects/{id}/events/ws``), subscribed from
the last handled cursor so a restart replays what the gateway missed. Outbound: Photon's REST API
through its Python SDK. Both clients come from the ``photon`` extra (``photonhq-api`` and
``python-socketio``); there is no sidecar and no public URL. A shared iMessage line answers only
in direct conversations, and only people who have texted it since their line assignment.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms._shared import seed_extra_from_env as _seed_extra_from_env
from gateway.platforms._shared import send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, bounded_put, cancel_task, compile_mention_patterns
from gateway.platforms.helpers import strip_markdown
from gateway.platforms.send_targets import regex_stripped_parser
from hermes_constants import get_hermes_home
from utils import atomic_json_write

from . import auth as photon_auth

try:
    import socketio
    from photon_api import ApiError, AsyncPhoton, PhotonError, TransportError
except ImportError:  # the `photon` extra installs on first connect, see ensure_photon_deps()
    socketio = None
    AsyncPhoton = None

    class PhotonError(Exception):  # type: ignore[no-redef]
        """Placeholder until ensure_photon_deps() binds the SDK's PhotonError."""

    class ApiError(PhotonError):  # type: ignore[no-redef]
        """Placeholder until ensure_photon_deps() binds the SDK's ApiError."""

    class TransportError(PhotonError):  # type: ignore[no-redef]
        """Placeholder until ensure_photon_deps() binds the SDK's TransportError."""

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 8000  # iMessage caps practical size at ~16 KB; conservative, matches BlueBubbles
_DEDUP_MAX_SIZE = 4000  # a resubscribe replays every event after the saved cursor
_DEDUP_WINDOW_SECONDS = 48 * 3600
_TYPING_COOLDOWN_SECONDS = 5.0  # per chat; Photon's typing state is refreshed by repeating it
# Group-chat wake words — same defaults as BlueBubbles so both iMessage adapters gate alike.
_DEFAULT_MENTION_PATTERNS = [r"(?<![\w@])@?hermes\s+agent\b[,:\-]?", r"(?<![\w@])@?hermes\b[,:\-]?"]
_EVENT_API_VERSION = "2026-07-01"  # the event contract the parsing below reads
_CONNECT_TIMEOUT_SECONDS = 15.0
_SUBSCRIBE_TIMEOUT_SECONDS = 10.0
_RECONNECT_DELAY_MAX_SECONDS = 60.0
_MAX_INBOUND_ATTACHMENT_BYTES = 64 * 1024 * 1024
_ROUTE_SCAN_PAGES = 5  # newest received messages scanned (100 a page) to find a phone's conversation
_ROUTES_MAX = 500
_SENT_IDS_MAX = 1000
_KEY_REJECTED_STATUSES = (401, 403, 404)
_SPACE_ID_RE = re.compile(r"^pho_spc_[0-7][0-9a-hjkmnp-tv-z]{25}$")
# Content types that describe a conversation rather than say anything in it.
_SIGNAL_CONTENT_TYPES = {"message.read", "space.typing.update", "space.background.update"}
_CURSOR_FILE = "photon_events.json"


# -- Dependencies (the `photon` extra) ----------------------------------------------------

def photon_deps_present() -> bool:
    """PASSIVE probe for the registry's ``check_fn``: never installs."""
    return AsyncPhoton is not None and socketio is not None


def ensure_photon_deps() -> bool:
    """ACTIVE installer (``ensure_deps_fn``): install the ``photon`` extra on first use and bind it."""
    if photon_deps_present():
        return True

    def _import() -> dict[str, Any]:
        import socketio
        from photon_api import ApiError, AsyncPhoton, PhotonError, TransportError
        return {"socketio": socketio, "AsyncPhoton": AsyncPhoton, "ApiError": ApiError,
                "PhotonError": PhotonError, "TransportError": TransportError}

    from pm.extras import ensure_and_bind
    return ensure_and_bind("photon", _import, globals()) and photon_auth.ensure_sdk()


# -- Configuration --------------------------------------------------------------------------

def _credentials(extra: Optional[dict] = None) -> tuple[str, str]:
    """``(project_id, api_key)``: the profile's env and ``auth.json`` first, then ``config.extra``."""
    project_id, api_key = photon_auth.load_runtime_credentials()
    extra = extra or {}
    extra_id = str(extra.get("project_id") or "")
    if not project_id and extra_id.startswith(photon_auth.PROJECT_ID_PREFIX):
        project_id = extra_id
    return project_id or "", api_key or str(extra.get("project_api_key") or "")


def validate_config(cfg: PlatformConfig) -> bool:
    return all(_credentials(cfg.extra))


def is_connected(cfg: PlatformConfig) -> bool:
    return validate_config(cfg)


def _env_enablement() -> Optional[dict]:
    """``env_enablement_fn``: seed ``PlatformConfig.extra`` so env-only setups appear in status."""
    project_id, api_key = photon_auth.load_runtime_credentials()
    if not (project_id and api_key):
        return None
    return {"project_id": project_id, "project_api_key": api_key,
            **_seed_extra_from_env((), home_env="PHOTON_HOME_CHANNEL")}


def _env_flag(name: str, default: str) -> bool:
    return str(_get_scoped_secret(name, default)).strip().lower() not in {"false", "0", "no", "off"}


def _markdown_enabled() -> bool:
    """Replies go out as markdown; ``PHOTON_MARKDOWN=false`` is the kill-switch to plain text."""
    return _env_flag("PHOTON_MARKDOWN", "true")


def _plain(text: str) -> str:
    return strip_markdown(text, keep_link_targets=True)


# -- Event cursor -----------------------------------------------------------------------------
# One profile runs one Photon project, so the file holds that project's last handled cursor; a
# different project id starts live again.

def _cursor_path() -> Path:
    return get_hermes_home() / _CURSOR_FILE


def _load_cursor(project_id: str) -> int:
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    seq = data.get("seq") if isinstance(data, dict) and data.get("project_id") == project_id else None
    return seq if isinstance(seq, int) and seq >= 0 else 0


def _save_cursor(project_id: str, seq: int) -> None:
    atomic_json_write(_cursor_path(), {"project_id": project_id, "seq": seq})


# -- Sent-text index (quoted context for threaded replies to our own messages) ---------------

async def _record_sent_text_async(space_id: Optional[str], message_id: Optional[str], text: Optional[str]) -> None:
    """Remember what we sent so a later threaded reply to it carries the quoted text. Never raises."""
    try:
        from gateway import rich_sent_store
        await rich_sent_store.record_async(space_id, message_id, text)
    except Exception:  # best-effort index: a failure only loses the quote
        logger.debug("[photon] sent-text index record failed", exc_info=True)


def _lookup_sent_text(space_id: Optional[str], message_id: Optional[str]) -> Optional[str]:
    try:
        from gateway import rich_sent_store
        return rich_sent_store.lookup(space_id, message_id)
    except Exception:  # best-effort index: a failure only loses the quote
        logger.debug("[photon] sent-text index lookup failed", exc_info=True)
        return None


# -- REST client --------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Route:
    """Where a send goes: the conversation and the agent user (the line) that speaks in it."""
    space_id: str
    sender_id: str


def _route_of(message: dict[str, Any]) -> Optional[_Route]:
    """A stored Message's route: the agent is ``receivedBy`` on what we received, ``sentBy`` on what we sent."""
    space_id = (message.get("space") or {}).get("id")
    agent = message.get("receivedBy") if message.get("status") == "received" else message.get("sentBy")
    agent_id = (agent or {}).get("id")
    return _Route(space_id, agent_id) if space_id and agent_id else None


class _PhotonApi:
    """The REST calls the adapter and the standalone sender share. Each call opens its own SDK
    client: a send can run on another thread's event loop (``send_message``), and a pooled HTTP
    client is bound to the loop that created it."""

    def __init__(self, project_id: str, api_key: str) -> None:
        self.project_id = project_id
        self._api_key = api_key
        self._routes: dict[str, _Route] = {}

    async def _call(self, method: str, input_name: str, payload: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with AsyncPhoton(base_url=photon_auth._api_host(), headers=headers, timeout=30.0) as photon:
            return await getattr(photon.projects, method)(photon_auth._rpc(input_name, payload))

    async def check_access(self) -> None:
        await self._call("get", "GetProjectInput", {"path": {"projectId": self.project_id}})

    async def _list_messages(self, query: dict[str, Any]) -> dict[str, Any]:
        page = await self._call("list_messages", "ListMessagesInput", {"path": {"id": self.project_id}, "query": query})
        return photon_auth._data(page)

    def remember(self, route: _Route, handle: Optional[str] = None) -> None:
        bounded_put(self._routes, route.space_id, route, _ROUTES_MAX)
        if handle:
            bounded_put(self._routes, photon_auth._normalize_phone(handle), route, _ROUTES_MAX)

    async def route(self, target: str) -> _Route:
        """The route for a space id or an E.164 number, from what the line has already exchanged:
        a shared line can only text someone who texted it first, so that conversation exists."""
        key = target.strip()
        if not key.startswith("pho_spc_"):
            key = photon_auth._normalize_phone(key)
            if not photon_auth.E164_RE.match(key):
                raise LookupError("not a Photon space id (pho_spc_…) or an E.164 phone number")
        cached = self._routes.get(key)
        if cached:
            return cached
        by_space = key.startswith("pho_spc_")
        query: dict[str, Any] = {"order": "desc", "platform": "imessage"}
        query.update({"spaceId": key, "pageSize": 1} if by_space else {"direction": "received", "pageSize": 100})
        for _ in range(_ROUTE_SCAN_PAGES):
            page = await self._list_messages(query)
            for message in page.get("items") or []:
                handle = (message.get("sentBy") or {}).get("handle") or ""
                route = _route_of(message)
                if route and (by_space or photon_auth._normalize_phone(handle) == key):
                    self.remember(route, None if by_space else handle)
                    return route
            token = page.get("nextPageToken")
            if by_space or not isinstance(token, str):
                break
            query["pageToken"] = token
        raise LookupError("no iMessage conversation with this target on the Photon project yet; "
                          "a shared line can only text someone who has texted it first")

    async def send(self, route: _Route, content: dict[str, Any]) -> str:
        payload = {"path": {"id": self.project_id}, "headers": {"idempotency-key": f"hermes:{uuid.uuid4()}"},
                   "body": {"spaceId": route.space_id, "senderId": route.sender_id, "content": content}}
        sent = photon_auth._data(await self._call("send_message", "SendMessageInput", payload))
        return str(sent.get("id") or "")

    async def send_text(self, route: _Route, text: str) -> str:
        """Markdown when enabled; Photon's content validation refusing it falls back to plain text."""
        if _markdown_enabled():
            try:
                return await self.send(route, {"type": "text", "format": "markdown", "text": text})
            except ApiError as e:
                if photon_auth.problem_code(e) != "INVALID_CONTENT":
                    raise
                logger.info("[photon] Photon refused the markdown reply; resending it as plain text")
        return await self.send(route, {"type": "text", "format": "plain", "text": _plain(text)})

    async def send_image(self, route: _Route, path: str, caption: Optional[str] = None) -> str:
        data = await asyncio.to_thread(Path(path).read_bytes)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # multipart/related, exactly two parts: the attachment's JSON description, then its bytes.
        boundary = f"hermes-{uuid.uuid4().hex}"
        meta = json.dumps({"contentType": mime, "sizeBytes": len(data), "filename": Path(path).name})
        body = b"".join((
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{meta}\r\n".encode(),
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode(), data, f"\r\n--{boundary}--\r\n".encode()))
        payload = {"path": {"id": self.project_id}, "body": body, "headers": {
            "content-length": str(len(body)), "idempotency-key": f"hermes:{uuid.uuid4()}",
            "content-type": f'multipart/related; boundary={boundary}; type="application/json"'}}
        attachment = photon_auth._data(await self._call("upload_attachment", "UploadAttachmentInput", payload))
        content: dict[str, Any] = {"type": "attachment", "attachmentId": attachment["id"]}
        if caption:
            content["metadata"] = {"caption": {"format": "plain", "text": _plain(caption)}}
        return await self.send(route, content)

    async def download(self, attachment_id: str) -> tuple[Optional[bytes], str, str]:
        """``(bytes, content_type, filename)``; bytes are None past the inbound size cap."""
        path = {"id": self.project_id, "attachmentId": attachment_id}
        meta = photon_auth._data(await self._call("get_attachment", "GetAttachmentInput", {"path": path}))
        mime, name = str(meta.get("contentType") or ""), str(meta.get("filename") or attachment_id)
        if int(meta.get("sizeBytes") or 0) > _MAX_INBOUND_ATTACHMENT_BYTES:
            return None, mime, name
        return await self._call("download_attachment", "DownloadAttachmentInput", {"path": path}), mime, name

    async def set_typing(self, route: _Route, typing: bool) -> None:
        await self._call("set_space_typing", "SetSpaceTypingInput", {
            "path": {"id": self.project_id, "spaceId": route.space_id},
            "body": {"senderId": route.sender_id, "typing": typing}})

    async def mark_read(self, message_id: str) -> None:
        await self._call("mark_message_read", "MarkMessageReadInput", {
            "path": {"id": self.project_id, "messageId": message_id},
            "headers": {"idempotency-key": f"hermes-read:{message_id}"}})


def _send_failure(exc: BaseException) -> SendResult:
    """A failed send: Photon's 4xx refusals and unknown targets are final (a retry or the plain-text
    resend would fail the same way); timeouts, 429 and 5xx are retryable."""
    if isinstance(exc, ApiError):
        code = photon_auth.problem_code(exc) or f"HTTP {exc.status}"
        transient = exc.status in (408, 429) or exc.status >= 500
        return SendResult(success=False, error=f"Photon refused the send ({code}): {exc}", retryable=transient,
                          raw_response={"status": exc.status, "code": code, "final": not transient})
    if isinstance(exc, LookupError):
        return SendResult(success=False, error=str(exc), raw_response={"final": True})
    cause = f": {exc.__cause__}" if exc.__cause__ else ""
    return SendResult(success=False, error=f"Photon request failed{cause}", retryable=True)


# -- Inbound content ------------------------------------------------------------------------------

_Normalized = tuple[str, MessageType, list[str], list[str]]  # text, type, media_urls, media_types


def _parse_timestamp(ts_str: Any) -> datetime:
    try:
        return datetime.fromisoformat(ts_str) if isinstance(ts_str, str) and ts_str else datetime.now(tz=UTC)
    except ValueError:
        return datetime.now(tz=UTC)


def _content_text(content: dict[str, Any]) -> str:
    """The typed text of a content payload WITHOUT fetching attachments, for the group mention gate."""
    if content.get("type") == "text":
        return content.get("text") or ""
    if content.get("type") == "group":
        return "\n".join(filter(None, (_content_text(p) for p in content.get("parts") or [] if isinstance(p, dict))))
    return ""


def _merge_parts(parts: list[_Normalized]) -> _Normalized:
    """One event for a group payload: texts joined, media in order, the first media part's type."""
    texts = [text for text, mtype, urls, _ in parts if mtype == MessageType.TEXT or not urls]
    media_urls = [url for *_, urls, _ in parts for url in urls]
    media_types = [mime for *_, mimes in parts for mime in mimes]
    mtype = next((m for _, m, urls, _ in parts if urls), MessageType.TEXT)
    text = "\n".join(t for t in texts if t).strip()
    return text or ("(attachment)" if media_urls else "[Photon empty group received]"), mtype, media_urls, media_types


def _attachment_message_type(mime: str) -> MessageType:
    mime = (mime or "").lower()
    for prefix, mtype in (("image/", MessageType.PHOTO), ("video/", MessageType.VIDEO), ("audio/", MessageType.AUDIO)):
        if mime.startswith(prefix):
            return mtype
    return MessageType.DOCUMENT


# MIME → extension maps for cached inbound bytes (mirror BlueBubbles naming).
_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/heic": ".jpg", "image/heif": ".jpg", "image/tiff": ".jpg"}
_AUDIO_EXT_BY_MIME = {
    "audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "audio/x-caf": ".caf", "audio/mp4": ".m4a", "audio/aac": ".m4a"}


def _cache_attachment(raw: bytes, name: str, mime: str, *, is_voice: bool) -> Optional[str]:
    """Write inbound bytes into the shared media cache by MIME; None when caching fails."""
    from gateway.platforms.base import cache_audio_from_bytes, cache_document_from_bytes, cache_image_from_bytes
    mime = (mime or "").lower()
    suffix = Path(name).suffix if name else ""  # prefer the real extension
    try:
        if mime.startswith("image/"):
            try:
                return cache_image_from_bytes(raw, suffix or _IMAGE_EXT_BY_MIME.get(mime, ".jpg"))
            except ValueError:  # unsupported image bytes (e.g. HEIC magic): deliver as a document
                return cache_document_from_bytes(raw, name)
        if is_voice or mime.startswith("audio/"):
            return cache_audio_from_bytes(raw, suffix or _AUDIO_EXT_BY_MIME.get(mime, ".m4a" if is_voice else ".mp3"))
        return cache_document_from_bytes(raw, name)  # video, application/*, everything else
    except (OSError, ValueError) as exc:
        logger.warning("[photon] failed to cache inbound attachment %s: %s", name, exc)
        return None


# -- Adapter --------------------------------------------------------------------------------------

class _SubscribeRefused(RuntimeError):
    """Photon answered ``subscribe`` with ``ok: false``."""

    def __init__(self, error: Any) -> None:
        error = error if isinstance(error, dict) else {}
        self.code = str(error.get("code") or "unknown")
        self.retryable = error.get("retryable") is not False
        super().__init__(f"{self.code}: {error.get('message') or 'subscription refused'}")


class PhotonAdapter(BasePlatformAdapter):
    """Bidirectional bridge to Photon 2.0: Socket.IO events in, REST sends out."""

    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH
    SUPPORTS_MESSAGE_EDITING = False  # no edit API: streaming must not leave a stale cursor (▉)
    serial_receive_loop = True  # one worker awaits each delivery's handle_message before the next

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("photon"))
        extra = config.extra or {}
        self._project_id, self._project_api_key = _credentials(extra)
        # iMessage renders no code blocks; never advertise fences.
        self.supports_code_blocks = False
        self._api: Optional[_PhotonApi] = None
        self._sio: Any = None
        self._stream_running = False
        self._stream_closed = asyncio.Event()
        self._generation = 0  # bumped on every disconnect: queued deliveries of a dead session are dropped
        self._cursor = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stream_task: Optional[asyncio.Task] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(max_size=_DEDUP_MAX_SIZE, ttl_seconds=_DEDUP_WINDOW_SECONDS)
        self._sent_message_ids: dict[str, float] = {}
        self._typing_last_sent: dict[str, float] = {}
        # Group-chat mention gating (parity with BlueBubbles); DMs are never gated.
        require_mention = extra.get("require_mention")
        if require_mention is None:
            require_mention = _get_scoped_secret("PHOTON_REQUIRE_MENTION")
        self.require_mention = str(require_mention).strip().lower() in {"true", "1", "yes", "on"}
        self._mention_patterns = compile_mention_patterns(
            extra["mention_patterns"] if "mention_patterns" in extra else _get_scoped_secret("PHOTON_MENTION_PATTERNS"),
            log_prefix="photon", defaults=_DEFAULT_MENTION_PATTERNS, logger_=logger)

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return bool(text) and any(pattern.search(text) for pattern in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:
        """Strip a leading wake word only (patterns are regexes; never touch later words)."""
        stripped = text.lstrip() if text else ""
        for match in filter(None, (pattern.match(stripped) for pattern in self._mention_patterns)):
            return stripped[match.end():].lstrip(" ,:-") or text
        return text

    # -- Connection lifecycle -------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not ensure_photon_deps():
            self._set_fatal_error("MISSING_DEP", "Photon's Python client (the `photon` extra) is not installed",
                                  retryable=False)
            return False
        if not (self._project_id and self._project_api_key):
            if photon_auth.has_legacy_credentials():
                logger.warning("[photon] %s", photon_auth.LEGACY_NOTICE)
                self._set_fatal_error("SPECTRUM_LEGACY", photon_auth.LEGACY_NOTICE, retryable=False)
            else:
                self._set_fatal_error("MISSING_CREDENTIALS",
                                      "PHOTON_PROJECT_ID and PHOTON_API_KEY are required. Run: hermes photon setup",
                                      retryable=False)
            return False
        self._api = _PhotonApi(self._project_id, self._project_api_key)
        problem = await self._key_problem()
        if problem is not None:
            code, message, retryable = problem
            self._set_fatal_error(code, message, retryable=retryable)
            return False
        self._cursor = await asyncio.to_thread(_load_cursor, self._project_id)
        self._stream_running = True
        try:
            await self._open_session()
        except (_SubscribeRefused, OSError, socketio.exceptions.SocketIOError) as e:
            self._stream_running = False
            retryable = not isinstance(e, _SubscribeRefused) or e.retryable
            self._set_fatal_error("STREAM_FAILED", f"could not open Photon's event stream: {e}", retryable=retryable)
            return False
        self._worker_task = asyncio.create_task(self._process_deliveries())
        self._stream_task = asyncio.create_task(self._supervise_stream())
        self._mark_connected()
        logger.info("[photon] connected — streaming project %s events after cursor %d", self._project_id, self._cursor)
        self._wire_plugin_handlers(None)  # ctx.register_platform_handler natives
        return True

    async def _key_problem(self) -> Optional[tuple[str, str, bool]]:
        """Probe the key: None when Photon accepts it, else ``(code, message, retryable)``. A rejected
        key is final (re-run setup) rather than a reconnect loop."""
        try:
            await self._api.check_access()
        except ApiError as e:
            if e.status in _KEY_REJECTED_STATUSES:
                return ("AUTH_REJECTED", f"Photon rejected PHOTON_API_KEY for project {self._project_id} "
                        f"(HTTP {e.status}). Run: hermes photon setup", False)
            return "CONNECT_FAILED", f"Photon API error: {e}", True
        except PhotonError as e:
            return "CONNECT_FAILED", f"could not reach Photon: {e}", True
        return None

    async def _open_session(self) -> None:
        """Connect a fresh Socket.IO client and subscribe from the saved cursor; raises on failure.
        Reconnection is ours (``_supervise_stream``), so every session subscribes exactly once."""
        sio = socketio.AsyncClient(reconnection=False, handle_sigint=False)
        generation = self._generation
        sio.on("delivery", lambda event, metadata=None: self._queue.put_nowait((generation, event, metadata or {})))
        sio.on("delivery_error", self._on_delivery_error)
        sio.on("disconnect", lambda *_reason: self._on_disconnect(sio))
        try:
            await sio.connect(
                photon_auth._api_host(), headers={"Authorization": f"Bearer {self._project_api_key}"},
                transports=["websocket"], socketio_path=f"/v1/projects/{self._project_id}/events/ws",
                wait_timeout=_CONNECT_TIMEOUT_SECONDS)
            ack = await sio.call("subscribe", {"startSeq": self._cursor, "apiVersion": _EVENT_API_VERSION},
                                 timeout=_SUBSCRIBE_TIMEOUT_SECONDS)
            if not (isinstance(ack, dict) and ack.get("ok") is True):
                raise _SubscribeRefused(ack.get("error") if isinstance(ack, dict) else None)
        except BaseException:
            with contextlib.suppress(OSError, socketio.exceptions.SocketIOError):
                await sio.disconnect()
            raise
        self._sio = sio
        self._stream_closed.clear()

    def _on_disconnect(self, sio: Any) -> None:
        if sio is self._sio:
            self._sio = None
            self._generation += 1
            self._stream_closed.set()

    async def _supervise_stream(self) -> None:
        """Reopen the stream whenever it closes, with jittered backoff; each new session
        resubscribes from the last handled cursor, so nothing between them is lost."""
        delay = 1.0
        while self._stream_running:
            await self._stream_closed.wait()
            if not self._stream_running:
                return
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            if not self._stream_running:
                return
            try:
                await self._open_session()
            except _SubscribeRefused as e:
                if not e.retryable:
                    await self._stop_stream_fatally(f"STREAM_{e.code.upper()}", f"Photon refused the event subscription: {e}")
                    return
                logger.warning("[photon] event subscription refused (%s); retrying in %.0fs", e, delay)
            except (OSError, socketio.exceptions.SocketIOError) as e:
                logger.warning("[photon] event stream reconnect failed (%s); retrying in %.0fs", e, delay)
                problem = await self._key_problem() if isinstance(e, socketio.exceptions.ConnectionError) else None
                if problem is not None and not problem[2]:
                    await self._stop_stream_fatally(problem[0], problem[1])
                    return
            else:
                logger.info("[photon] event stream reconnected after cursor %d", self._cursor)
                delay = 1.0
                continue
            delay = min(delay * 2, _RECONNECT_DELAY_MAX_SECONDS)

    async def _stop_stream_fatally(self, code: str, message: str) -> None:
        logger.error("[photon] %s", message)
        self._stream_running = False
        self._set_fatal_error(code, message, retryable=False)
        await self._notify_fatal_error()

    async def _on_delivery_error(self, error: Any = None) -> None:
        error = error if isinstance(error, dict) else {}
        code, message = str(error.get("code") or "unknown"), str(error.get("message") or "")
        if error.get("fatal") and error.get("retryable") is False:
            await self._stop_stream_fatally(f"STREAM_{code.upper()}", f"Photon closed the event stream: {code}: {message}")
            return
        logger.warning("[photon] event stream reported %s: %s", code, message)

    async def disconnect(self) -> None:
        self._stream_running = False
        self._stream_closed.set()  # wake the supervisor so it sees the stop
        sio, self._sio = self._sio, None
        if sio is not None:
            with contextlib.suppress(OSError, socketio.exceptions.SocketIOError):
                await sio.disconnect()
        for name in ("_stream_task", "_worker_task"):
            task = getattr(self, name)
            setattr(self, name, None)
            await cancel_task(task)
        self._mark_disconnected()

    # -- Inbound ---------------------------------------------------------------------------------

    async def _process_deliveries(self) -> None:
        """Hand deliveries to the gateway one at a time in cursor order, checkpointing each."""
        while True:
            generation, event, metadata = await self._queue.get()
            seq = metadata.get("seq") if isinstance(metadata, dict) else None
            if generation != self._generation or not isinstance(seq, int) or seq <= self._cursor:
                continue  # a dead session's backlog replays after the resubscribe
            try:
                await self._dispatch_event(event if isinstance(event, dict) else {})
            except Exception:  # a handler bug must not stall the stream on one event: log it and move on
                logger.exception("[photon] could not handle event %s", (event or {}).get("id"))
            self._cursor = seq
            try:
                await asyncio.to_thread(_save_cursor, self._project_id, seq)
            except OSError as e:
                logger.warning("[photon] could not save the event cursor: %s", e)

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "message.received":
            logger.debug("[photon] ignoring %s event", event.get("type"))
            return
        message = (event.get("data") or {}).get("object") or {}
        message_id = message.get("id")
        if message.get("platform") != "imessage" or not message_id or self._dedup.is_duplicate(message_id):
            return
        route = _route_of(message)
        if route is None:
            logger.warning("[photon] received message %s has no space or agent; ignoring it", message_id)
            return
        handle = (message.get("sentBy") or {}).get("handle") or ""
        self._api.remember(route, handle)
        await self._deliver_message(message, route, handle)
        if _env_flag("PHOTON_READ_RECEIPTS", "true"):
            try:
                await self._api.mark_read(message_id)
            except PhotonError as e:
                logger.debug("[photon] read receipt for %s failed: %s", message_id, e)

    async def _deliver_message(self, message: dict[str, Any], route: _Route, handle: str) -> None:
        content = message.get("content") or {}
        if content.get("type") in _SIGNAL_CONTENT_TYPES:
            logger.debug("[photon] ignoring %s content", content.get("type"))
            return
        chat_type = "dm" if (message.get("space") or {}).get("kind") == "direct" else "group"
        # Mention gate BEFORE normalising: a dropped group message must not download attachments.
        gated = chat_type == "group" and self.require_mention
        if gated and not self._message_matches_mention_patterns(_content_text(content)):
            logger.debug("[photon] ignoring group message (require_mention=true, no mention pattern matched)")
            return
        text, mtype, media_urls, media_types = await self._normalize(content)
        if gated:
            text = self._clean_mention_text(text)
        reply: dict[str, Any] = {}
        parent_id = message.get("parentMessageId")
        if parent_id:
            stored = _lookup_sent_text(route.space_id, parent_id)
            reply = {"reply_to_message_id": parent_id, "reply_to_text": stored,
                     "reply_to_is_own_message": parent_id in self._sent_message_ids or stored is not None}
        user_id = handle or (message.get("sentBy") or {}).get("id")
        source = self.build_source(chat_id=route.space_id, chat_name=route.space_id, chat_type=chat_type,
                                   user_id=user_id, user_name=user_id or None, message_id=message["id"])
        await self.handle_message(MessageEvent(
            text=text, message_type=mtype, source=source, message_id=message["id"], raw_message=message,
            timestamp=_parse_timestamp(message.get("createdAt")), media_urls=media_urls,
            media_types=media_types, **reply))

    async def _normalize(self, content: dict[str, Any]) -> _Normalized:
        ctype = content.get("type")
        if ctype == "text":
            return content.get("text") or "", MessageType.TEXT, [], []
        if ctype in ("attachment", "voice"):
            return await self._fetch_attachment(content)
        if ctype == "group":
            parts = [p for p in content.get("parts") or []
                     if isinstance(p, dict) and p.get("type") not in _SIGNAL_CONTENT_TYPES]
            return _merge_parts([await self._normalize(p) for p in parts])
        return f"[Photon content type not handled: {ctype}]", MessageType.TEXT, [], []

    async def _fetch_attachment(self, content: dict[str, Any]) -> _Normalized:
        attachment_id = str(content.get("attachmentId") or "")
        try:
            data, mime, name = await self._api.download(attachment_id)
        except PhotonError as e:
            logger.warning("[photon] could not download attachment %s: %s", attachment_id, e)
            data, mime, name = None, "", attachment_id
        # iMessage voice notes are CAF, so the MIME and name are signals too.
        is_voice = content.get("type") == "voice" or mime == "audio/x-caf" or name.lower().endswith(".caf")
        mtype = MessageType.VOICE if is_voice else _attachment_message_type(mime)
        label = "voice" if is_voice else "attachment"
        cached = await asyncio.to_thread(_cache_attachment, data, name, mime, is_voice=is_voice) if data else None
        if cached:
            return f"({label})", mtype, [cached], [mime or ("audio/mp4" if is_voice else "application/octet-stream")]
        return f"[Photon {label} received: {name} ({mime or 'unknown MIME'})]", mtype, [], []

    # -- Outbound --------------------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        return content if _markdown_enabled() else _plain(content)

    async def _deliver(self, chat_id: str, send: Callable[[_PhotonApi, _Route], Any], *, sent_text: str) -> SendResult:
        api = self._api
        if api is None:
            return SendResult(success=False, error="Photon adapter not connected", retryable=True)
        try:
            route = await api.route(chat_id)
            message_id = await send(api, route)
        except (PhotonError, LookupError, OSError) as e:
            return _send_failure(e)
        if message_id:
            bounded_put(self._sent_message_ids, message_id, time.time(), _SENT_IDS_MAX)
        await _record_sent_text_async(route.space_id, message_id, sent_text)
        return SendResult(success=True, message_id=message_id)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        text = self.format_message(content)
        if len(text) > self.MAX_MESSAGE_LENGTH:
            logger.warning("[photon] truncating outbound from %d to %d chars", len(text), self.MAX_MESSAGE_LENGTH)
            text = text[: self.MAX_MESSAGE_LENGTH]
        return await self._deliver(chat_id, lambda api, route: api.send_text(route, text), sent_text=text)

    def _send_retry_is_final(self, result: SendResult) -> bool:
        raw = result.raw_response
        return isinstance(raw, dict) and raw.get("final") is True

    # Photon 2.0 iMessage sends text and images only: video, voice notes and documents fall back
    # to the base adapter's notice.

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        from gateway.platforms.base import cache_image_from_url
        try:
            local_path = await cache_image_from_url(image_url)
        except Exception:  # unfetchable URL: send it as text instead
            logger.warning("[photon] could not fetch an image; sending the link instead", exc_info=True)
            return await super().send_image(chat_id, image_url, caption, reply_to)
        return await self.send_image_file(chat_id, local_path, caption=caption)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        safe_path = self.validate_media_delivery_path(str(image_path))  # cron may pass arbitrary strings
        if not safe_path:
            return SendResult(success=False, error=f"unsafe or missing attachment path: {image_path}")
        return await self._deliver(chat_id, lambda api, route: api.send_image(route, safe_path, caption),
                                   sent_text=caption or f"[image: {Path(safe_path).name}]")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        now = time.monotonic()
        last = self._typing_last_sent.get(chat_id)
        if last is not None and now - last < _TYPING_COOLDOWN_SECONDS:
            return
        self._typing_last_sent[chat_id] = now
        await self._set_typing(chat_id, True)

    async def stop_typing(self, chat_id: str) -> None:
        self._typing_last_sent.pop(chat_id, None)
        await self._set_typing(chat_id, False)

    async def _set_typing(self, chat_id: str, typing: bool) -> None:
        api = self._api
        if api is None:
            return
        try:
            await api.set_typing(await api.route(chat_id), typing)
        except (PhotonError, LookupError) as e:
            logger.debug("[photon] typing=%s failed: %s", typing, e)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Photon's space id is opaque, and shared lines only serve direct conversations."""
        return {"name": chat_id, "type": "dm", "id": chat_id}


# -- Standalone (out-of-process) send for cron deliveries when the gateway is not co-resident -----

def _is_image(path: str) -> bool:
    return (mimetypes.guess_type(path)[0] or "").startswith("image/")


async def _standalone_send(
    pconfig: PlatformConfig, chat_id: str, message: str, *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    if not ensure_photon_deps():
        return send_error("Photon's Python client (the `photon` extra) is not installed")
    project_id, api_key = _credentials(pconfig.extra)
    if not (project_id and api_key):
        return send_error("PHOTON_PROJECT_ID and PHOTON_API_KEY are required. Run: hermes photon setup")
    api = _PhotonApi(project_id, api_key)
    last_message_id: Optional[str] = None
    try:
        route = await api.route(chat_id)
        if message:  # text first, so it leads the conversation
            text = (message if _markdown_enabled() else _plain(message))[:_MAX_MESSAGE_LENGTH]
            last_message_id = await api.send_text(route, text)
            await _record_sent_text_async(route.space_id, last_message_id, text)
        # media_files is List[Tuple[path, is_voice]] (filter_media_delivery_paths).
        for media_path, _is_voice in media_files or []:
            safe_path = BasePlatformAdapter.validate_media_delivery_path(str(media_path))
            if not safe_path or not _is_image(safe_path):
                logger.warning("[photon] standalone send skipped an attachment: Photon 2.0 iMessage sends images only")
                continue
            last_message_id = await api.send_image(route, safe_path)
            await _record_sent_text_async(route.space_id, last_message_id, f"[image: {Path(safe_path).name}]")
    except (PhotonError, LookupError, OSError) as e:
        return send_error(_send_failure(e).error)
    return {"success": True, "message_id": last_message_id}


# -- Plugin entry point ----------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by the Hermes plugin loader at startup."""
    from . import cli as _cli  # local: avoid argparse work at module load
    ctx.register_platform(
        target_ref_syntax_fn=regex_stripped_parser(_SPACE_ID_RE),  # space ids are adapter-native
        name="photon", label="iMessage via Photon", adapter_factory=lambda cfg: PhotonAdapter(cfg),
        dashboard_description="Use Hermes through iMessage via Photon 2.0.",
        docs_url="https://hermes-agent.nousresearch.com/docs/user-guide/messaging/photon",
        check_fn=photon_deps_present, ensure_deps_fn=ensure_photon_deps,
        validate_config=validate_config, is_connected=is_connected,
        required_env=["PHOTON_PROJECT_ID", "PHOTON_API_KEY"],
        install_hint=(
            "Run: hermes photon setup  (signs in to Photon 2.0, creates a project and its API key, "
            "and assigns your phone number a shared iMessage line)."),
        setup_fn=_cli.gateway_setup,  # surfaces Photon in the unified `hermes gateway setup` wizard
        env_enablement_fn=_env_enablement, cron_deliver_env_var="PHOTON_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, allowed_users_env="PHOTON_ALLOWED_USERS",
        allow_all_env="PHOTON_ALLOW_ALL_USERS", max_message_length=_MAX_MESSAGE_LENGTH, emoji="📱",
        pii_safe=True,  # E.164 phone numbers: redact session descriptions before they reach the LLM
        allow_update_command=True,
        platform_hint=(
            "You are texting via iMessage (Photon). Write like a person texting: short and conversational, "
            "answer first, no preamble or recap. Formatting mostly does not survive in iMessage, so no "
            "headers, tables, code fences or backticks. Put a command or code snippet on its own line as "
            "plain text so it copies and runs, and write links as bare URLs. You can send images natively: "
            "write MEDIA:/absolute/path/to/image in your response; other files, voice notes, reactions and "
            "polls are not available on this channel. Recipient identifiers are E.164 phone numbers; never "
            "expose them in responses unless the user asked."),
        # Permanent-message iMessage inboxes (no edit).
        display_tier="low",
    )
    ctx.register_cli_command(
        name="photon", help="Set up and manage the Photon iMessage integration",
        setup_fn=_cli.register_cli, handler_fn=_cli.dispatch)
