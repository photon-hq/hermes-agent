"""iMessage platform adapter.

Supports two modes:

  **Local** (default, macOS only): Reads the native iMessage SQLite
  database for inbound messages and sends via AppleScript.  Pure Python,
  no external dependencies beyond macOS system frameworks.  Requires
  Full Disk Access granted to the terminal running Hermes.

  **Remote**: Talks to a Photon ``advanced-imessage-http-proxy`` server
  over HTTP.  Works from any OS.  Provides file/image/audio sending,
  tapbacks, typing indicators, message effects, message queries, chat
  history, attachment download, and iMessage availability checks.

  API reference: https://github.com/photon-hq/advanced-imessage-http-proxy

Environment variables (remote — just your Photon credentials):
  IMESSAGE_SERVER_URL   — Photon endpoint from photon.codes
  IMESSAGE_API_KEY      — Photon API key from photon.codes

Environment variables (local — macOS only, no Photon account):
  IMESSAGE_ENABLED      — set to "true"

Config (config.yaml platforms section):
  imessage:
    enabled: true
    extra:
      local: true                       # false for remote mode
      server_url: ""                    # Photon endpoint from photon.codes
      poll_interval: 2.0                # seconds between polls
      database_path: ~/Library/Messages/chat.db
      group_policy: "open"              # "open" or "ignore"
      react_tapback: "love"             # tapback on inbound (remote only)
      done_tapback: ""                  # tapback after reply (remote only)
"""

import asyncio
import base64
import binascii
import json
import logging
import mimetypes
import os
import platform as platform_mod
import random
import sqlite3
import subprocess
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path.home() / "Library" / "Messages" / "chat.db")
_DEFAULT_PROXY_URL = "https://imessage-swagger.photon.codes"
_REMOTE_POLL_INTERVAL = 2.0
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0
MAX_MESSAGE_LENGTH = 20000

_AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav", ".aac", ".ogg", ".caf", ".opus"})

_VALID_EFFECTS = frozenset({
    "confetti", "fireworks", "balloon", "heart", "sparkles", "echo", "spotlight",
})
_VALID_TAPBACKS = frozenset({"love", "like", "dislike", "laugh", "emphasize", "question"})


def check_imessage_requirements() -> bool:
    """Check platform-level requirements (dependencies are stdlib + httpx)."""
    try:
        import httpx  # noqa: F401
        return True
    except ImportError:
        logger.warning("iMessage remote mode requires httpx: pip install httpx")
        return False


def _make_bearer_token(server_url: str, api_key: str) -> str:
    """Build the Bearer token expected by advanced-imessage-http-proxy.

    Token format: base64(server_url|api_key).
    If the key already decodes to a ``url|key`` pair it is used as-is.
    """
    try:
        decoded = base64.b64decode(api_key, validate=True).decode()
        if "|" in decoded:
            return api_key
    except (binascii.Error, UnicodeDecodeError, ValueError):
        pass
    raw = f"{server_url}|{api_key}"
    return base64.b64encode(raw.encode()).decode()


def _extract_address(chat_id: str) -> str:
    """Convert a chatGuid to the proxy's address format.

    ``iMessage;-;+1234567890`` -> ``+1234567890``
    ``iMessage;+;chat123``     -> ``group:chat123``
    ``+1234567890``            -> ``+1234567890`` (passthrough)
    """
    if ";-;" in chat_id:
        return chat_id.split(";-;", 1)[1]
    if ";+;" in chat_id:
        return "group:" + chat_id.split(";+;", 1)[1]
    return chat_id


def _split_paragraphs(text: str) -> List[str]:
    """Split text on paragraph boundaries, respecting max length."""
    parts: List[str] = []
    for para in text.split("\n\n"):
        stripped = para.strip()
        if stripped:
            if len(stripped) <= MAX_MESSAGE_LENGTH:
                parts.append(stripped)
            else:
                parts.extend(
                    BasePlatformAdapter.truncate_message(stripped, MAX_MESSAGE_LENGTH)
                )
    return parts or [text]


class IMessageAdapter(BasePlatformAdapter):
    """iMessage adapter with local (macOS) and remote (Photon proxy) modes.

    Remote mode supports: send text (replies, effects), send files
    (images, documents, audio), tapbacks, typing indicators, mark
    read/unread, message queries/search, chat listing, attachment
    download, and iMessage availability checks.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.IMESSAGE)
        extra = config.extra or {}
        self._local = extra.get("local", True)
        self._server_url = extra.get("server_url", "")
        self._api_key = config.api_key or ""
        self._poll_interval = float(extra.get("poll_interval", _REMOTE_POLL_INTERVAL))
        self._db_path = str(Path(extra.get("database_path", _DEFAULT_DB_PATH)).expanduser())
        self._group_policy = extra.get("group_policy", "open")
        self._react_tapback = extra.get("react_tapback", "love")
        self._done_tapback = extra.get("done_tapback", "")

        self._poll_task: Optional[asyncio.Task] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._last_rowid: int = 0
        self._consecutive_errors: int = 0
        self._typing_chats: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if self._local:
            return await self._connect_local()
        return await self._connect_remote()

    async def disconnect(self):
        self._mark_disconnected()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for chat_id in list(self._typing_chats):
            await self._api_stop_typing(chat_id)
        self._typing_chats.clear()
        if self._http:
            await self._http.aclose()
            self._http = None

    def _backoff_delay(self) -> float:
        """Exponential backoff with jitter for poll error recovery."""
        base = min(_BACKOFF_INITIAL * (2 ** (self._consecutive_errors - 1)), _BACKOFF_MAX)
        return base * (0.5 + random.random() * 0.5)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if self._local:
            return await self._send_local(chat_id, content)
        return await self._send_remote(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def send_typing(self, chat_id: str, metadata=None):
        if not self._local and self._http:
            try:
                await self._http.post(f"/chats/{chat_id}/typing")
            except httpx.HTTPError:
                pass

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, **kwargs) -> SendResult:
        local_path = await self._download_to_cache(image_url)
        if self._local:
            if local_path:
                try:
                    await self._applescript_send_file(chat_id, local_path)
                except Exception as exc:
                    logger.warning("iMessage local send_image AppleScript failed: %s", exc)
                    return SendResult(success=False, error=str(exc))
            elif not caption:
                return SendResult(success=False, error=f"Failed to download image: {image_url[:80]}")
        else:
            if local_path:
                result = await self._api_send_file(chat_id, local_path)
                if result:
                    if caption:
                        await self.send(chat_id, caption)
                    return SendResult(success=True)
            logger.debug("iMessage remote: file upload failed, sending URL/caption as text")
            if not caption and image_url:
                await self.send(chat_id, image_url)
                return SendResult(success=True)
        if caption:
            await self.send(chat_id, caption)
        return SendResult(success=True)

    async def _download_to_cache(self, url: str) -> Optional[str]:
        """Download URL to image cache, return local path or None."""
        try:
            from gateway.platforms.base import get_image_cache_dir
            cache_dir = get_image_cache_dir()
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            ext = mimetypes.guess_extension(resp.headers.get("content-type", "")) or ".jpg"
            import hashlib
            name = hashlib.sha256(url.encode()).hexdigest()[:16] + ext
            dest = cache_dir / name
            dest.write_bytes(resp.content)
            return str(dest)
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Failed to download image %s: %s", url[:80], e)
            return None

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            **kwargs) -> SendResult:
        if self._local:
            try:
                await self._applescript_send_file(chat_id, file_path)
            except Exception as exc:
                logger.warning("iMessage local send_document AppleScript failed: %s", exc)
                return SendResult(success=False, error=str(exc))
        else:
            result = await self._api_send_file(chat_id, file_path)
            if not result:
                logger.warning("iMessage remote: file upload failed for %s", Path(file_path).name)
                return SendResult(success=False, error=f"File upload failed: {Path(file_path).name}")
        if caption:
            await self.send(chat_id, caption)
        return SendResult(success=True)

    async def send_image_file(self, chat_id: str, file_path: str, caption: Optional[str] = None) -> SendResult:
        return await self.send_document(chat_id, file_path, caption)

    async def send_voice(self, chat_id: str, file_path: str) -> SendResult:
        if self._local:
            try:
                await self._applescript_send_file(chat_id, file_path)
            except Exception as exc:
                logger.warning("iMessage local send_voice AppleScript failed: %s", exc)
                return SendResult(success=False, error=str(exc))
        else:
            result = await self._api_send_file(chat_id, file_path, audio=True)
            if not result:
                logger.warning("iMessage remote: voice upload failed for %s", Path(file_path).name)
                return SendResult(success=False, error=f"Voice upload failed: {Path(file_path).name}")
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_group = chat_id.startswith("group:") or ";+;" in chat_id
        if not self._local and self._http:
            data = await self._api_get(f"/chats/{chat_id}")
            if isinstance(data, dict):
                return {
                    "name": data.get("displayName") or chat_id,
                    "type": "group" if is_group else "dm",
                    "chat_id": chat_id,
                }
        return {
            "name": chat_id,
            "type": "group" if is_group else "dm",
            "chat_id": chat_id,
        }

    # ==================================================================
    # LOCAL MODE  (macOS — sqlite3 + AppleScript)
    # ==================================================================

    async def _connect_local(self) -> bool:
        if platform_mod.system() != "Darwin":
            logger.error("iMessage local mode requires macOS")
            return False

        db_path = self._db_path
        if not Path(db_path).exists():
            logger.error(
                "iMessage database not found at %s. "
                "Ensure Full Disk Access is granted to your terminal.",
                db_path,
            )
            return False

        self._last_rowid = self._get_max_rowid(db_path)
        self._mark_connected()
        logger.info("iMessage local watcher started (polling %s)", db_path)
        self._poll_task = asyncio.create_task(self._local_poll_loop())
        return True

    async def _local_poll_loop(self):
        while self._running:
            try:
                await self._poll_local_db()
                self._consecutive_errors = 0
            except (sqlite3.Error, OSError) as e:
                self._consecutive_errors += 1
                delay = self._backoff_delay()
                logger.warning("iMessage local poll error (retry in %.1fs): %s", delay, e, exc_info=True)
                await asyncio.sleep(delay)
                continue
            except Exception as e:
                self._consecutive_errors += 1
                delay = self._backoff_delay()
                logger.error("iMessage local poll unexpected error (retry in %.1fs): %s", delay, e, exc_info=True)
                await asyncio.sleep(delay)
                continue
            await asyncio.sleep(max(0.5, self._poll_interval))

    def _get_max_rowid(self, db_path: str) -> int:
        try:
            with sqlite3.connect(db_path, uri=True) as conn:
                cur = conn.execute("SELECT MAX(ROWID) FROM message")
                row = cur.fetchone()
                return row[0] or 0
        except (sqlite3.Error, OSError):
            return 0

    async def _poll_local_db(self):
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, self._fetch_new_messages)
        for row in rows:
            await self._handle_local_message(row)
            self._last_rowid = max(self._last_rowid, int(row["ROWID"]))

    def _fetch_new_messages(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self._db_path, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT
                    m.ROWID,
                    m.guid,
                    m.text,
                    m.is_from_me,
                    m.date AS msg_date,
                    m.service,
                    h.id AS sender,
                    c.chat_identifier,
                    c.style AS chat_style,
                    a.ROWID AS att_rowid,
                    a.filename AS att_filename,
                    a.mime_type AS att_mime,
                    a.transfer_name AS att_transfer_name
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                LEFT JOIN chat c ON cmj.chat_id = c.ROWID
                LEFT JOIN message_attachment_join maj ON m.ROWID = maj.message_id
                LEFT JOIN attachment a ON maj.attachment_id = a.ROWID
                WHERE m.ROWID > ?
                ORDER BY m.ROWID ASC
                """,
                (self._last_rowid,),
            )
            msg_map: Dict[int, Dict[str, Any]] = {}
            for row in cur:
                d = dict(row)
                rowid = d["ROWID"]
                if rowid not in msg_map:
                    msg_map[rowid] = {**d, "attachments": []}
                if d.get("att_rowid"):
                    raw_path = d.get("att_filename") or ""
                    resolved = (
                        raw_path.replace("~", str(Path.home()), 1)
                        if raw_path.startswith("~")
                        else raw_path
                    )
                    msg_map[rowid]["attachments"].append({
                        "filename": resolved,
                        "mime_type": d.get("att_mime") or "",
                        "transfer_name": d.get("att_transfer_name") or "",
                    })
        return list(msg_map.values())

    async def _handle_local_message(self, row: Dict[str, Any]):
        if row.get("is_from_me"):
            return
        if (row.get("service") or "").lower() != "imessage":
            return

        message_id = row.get("guid", "")
        if self._is_seen(message_id):
            return

        sender = row.get("sender") or ""
        chat_id = row.get("chat_identifier") or sender
        content = row.get("text") or ""
        is_group = (row.get("chat_style") or 0) == 43

        if is_group and self._group_policy == "ignore":
            return

        image_paths: List[str] = []
        for att in row.get("attachments") or []:
            file_path = att.get("filename", "")
            if not file_path or not Path(file_path).exists():
                continue

            mime = att.get("mime_type") or ""
            ext = Path(file_path).suffix.lower()

            try:
                file_bytes = Path(file_path).read_bytes()
            except OSError as exc:
                logger.warning("iMessage local: cannot read attachment %s: %s", file_path, exc)
                continue

            if ext in _AUDIO_EXTENSIONS or mime.startswith("audio/"):
                cached = cache_audio_from_bytes(file_bytes, ext or ".m4a")
                content = f"{content}\n[Voice message: {cached}]" if content else f"[Voice message: {cached}]"
                continue

            if mime.startswith("image/"):
                cached = cache_image_from_bytes(file_bytes, ext or ".jpg")
                image_paths.append(cached)
            else:
                cached = cache_document_from_bytes(file_bytes, ext or ".bin")
                content = f"{content}\n[File: {cached}]" if content else f"[File: {cached}]"

        chat_type = "group" if is_group else "dm"
        source = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender,
        )
        msg_type = MessageType.PHOTO if image_paths else MessageType.TEXT
        event = MessageEvent(
            text=content,
            message_type=msg_type,
            source=source,
            media_urls=image_paths or [],
            media_types=["image/jpeg"] * len(image_paths) if image_paths else [],
            message_id=message_id,
            raw_message=row,
        )
        await self.handle_message(event)
        self._mark_seen(message_id)

    async def _send_local(self, chat_id: str, content: str) -> SendResult:
        try:
            for chunk in _split_paragraphs(content):
                await self._applescript_send_text(chat_id, chunk)
            return SendResult(success=True)
        except (subprocess.SubprocessError, OSError) as e:
            return SendResult(success=False, error=str(e))

    @staticmethod
    def _escape_applescript(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    async def _applescript_send_text(self, recipient: str, text: str):
        escaped_recipient = self._escape_applescript(recipient)
        escaped_text = self._escape_applescript(text)
        script = (
            f'tell application "Messages"\n'
            f"  set targetService to 1st account whose service type = iMessage\n"
            f'  set targetBuddy to participant "{escaped_recipient}" of targetService\n'
            f'  send "{escaped_text}" to targetBuddy\n'
            f"end tell"
        )
        await self._run_osascript(script)

    async def _applescript_send_file(self, recipient: str, file_path: str):
        escaped_recipient = self._escape_applescript(recipient)
        escaped_path = self._escape_applescript(file_path)
        script = (
            f'tell application "Messages"\n'
            f"  set targetService to 1st account whose service type = iMessage\n"
            f'  set targetBuddy to participant "{escaped_recipient}" of targetService\n'
            f'  send POSIX file "{escaped_path}" to targetBuddy\n'
            f"end tell"
        )
        await self._run_osascript(script)

    async def _run_osascript(self, script: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=15,
            ),
        )

    # ==================================================================
    # REMOTE MODE  (advanced-imessage-http-proxy)
    # https://github.com/photon-hq/advanced-imessage-http-proxy
    # ==================================================================

    async def _connect_remote(self) -> bool:
        if not self._server_url:
            logger.error("iMessage remote mode requires IMESSAGE_SERVER_URL")
            return False
        if not self._api_key:
            logger.error("iMessage remote mode requires IMESSAGE_API_KEY")
            return False

        token = _make_bearer_token(self._server_url, self._api_key)
        self._http = httpx.AsyncClient(
            base_url=_DEFAULT_PROXY_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        logger.info("iMessage remote: endpoint=%s", self._server_url)

        if not await self._api_health():
            logger.warning("iMessage server health check failed — will retry in poll loop")

        await self._seed_existing_ids()
        self._mark_connected()
        self._poll_task = asyncio.create_task(self._remote_poll_loop())
        logger.info("iMessage remote polling started (%ss interval)", self._poll_interval)
        return True

    async def _seed_existing_ids(self):
        """Mark existing messages so we only process new ones after startup."""
        try:
            resp = await self._api_get_messages(limit=100)
            for msg in resp:
                msg_id = msg.get("id") or msg.get("guid", "")
                if msg_id:
                    self._mark_seen(msg_id)
            logger.info("Seeded %d existing message IDs", len(self._processed_ids))
        except httpx.HTTPError as e:
            logger.debug("Could not seed existing message IDs: %s", e, exc_info=True)

    async def _remote_poll_loop(self):
        while self._running:
            try:
                await self._poll_remote()
                self._consecutive_errors = 0
            except httpx.HTTPError as e:
                self._consecutive_errors += 1
                delay = self._backoff_delay()
                logger.warning("iMessage remote poll error (retry in %.1fs): %s", delay, e, exc_info=True)
                await asyncio.sleep(delay)
                continue
            except Exception as e:
                self._consecutive_errors += 1
                delay = self._backoff_delay()
                logger.error("iMessage remote poll unexpected error (retry in %.1fs): %s", delay, e, exc_info=True)
                await asyncio.sleep(delay)
                continue
            await asyncio.sleep(max(0.5, self._poll_interval))

    async def _poll_remote(self):
        if not self._http:
            return
        messages = await self._api_get_messages(limit=50)
        for msg in reversed(messages):
            await self._handle_remote_message(msg)

    async def _handle_remote_message(self, data: Dict[str, Any]):
        sender_raw = data.get("from") or ""
        if sender_raw == "me" or data.get("isFromMe"):
            return

        message_id = data.get("id") or data.get("guid", "")
        if self._is_seen(message_id):
            return

        sender = sender_raw
        if not sender:
            handle = data.get("handle")
            if isinstance(handle, dict):
                sender = handle.get("address", "")

        address = data.get("chat") or sender
        if not address:
            chats = data.get("chats") or []
            chat_guid = chats[0].get("guid", "") if chats else ""
            address = _extract_address(chat_guid) if chat_guid else sender

        content = data.get("text") or ""
        is_group = address.startswith("group:") or (";+;" in address)

        if is_group and self._group_policy == "ignore":
            return

        image_paths: List[str] = []
        for att in data.get("attachments") or []:
            att_guid = att.get("id") or att.get("guid", "")
            name = att.get("name") or att.get("transferName") or att.get("filename") or ""
            if not att_guid or not self._http:
                continue

            local_path = await self._api_download_attachment(att_guid, name)
            if not local_path:
                continue

            mime, _ = mimetypes.guess_type(local_path)
            ext = Path(local_path).suffix.lower()

            if ext in _AUDIO_EXTENSIONS or (mime and mime.startswith("audio/")):
                cached = cache_audio_from_bytes(Path(local_path).read_bytes(), ext or ".m4a")
                content = f"{content}\n[Voice message: {cached}]" if content else f"[Voice message: {cached}]"
                continue

            if mime and mime.startswith("image/"):
                image_paths.append(local_path)
            else:
                content = f"{content}\n[File: {local_path}]" if content else f"[File: {local_path}]"

        chat_type = "group" if is_group else "dm"
        source = self.build_source(
            chat_id=address,
            chat_type=chat_type,
            user_id=sender,
        )
        msg_type = MessageType.PHOTO if image_paths else MessageType.TEXT
        ts_raw = data.get("sentAt") or data.get("dateCreated")
        ts = datetime.now()
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1e12 else ts_raw)
        elif isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                pass
        event = MessageEvent(
            text=content,
            message_type=msg_type,
            source=source,
            media_urls=image_paths or [],
            media_types=["image/jpeg"] * len(image_paths) if image_paths else [],
            message_id=message_id,
            timestamp=ts,
            raw_message=data,
        )
        await self.handle_message(event)
        self._mark_seen(message_id)

        if self._react_tapback and self._react_tapback in _VALID_TAPBACKS:
            await self._api_react(address, message_id, self._react_tapback)
        await self._api_mark_read(address)

    async def _send_remote(self, chat_id: str, content: str,
                           reply_to: Optional[str] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if not self._http:
            return SendResult(success=False, error="HTTP client not initialised")

        effect = (metadata or {}).get("effect", "")
        await self._api_start_typing(chat_id)
        try:
            for i, chunk in enumerate(_split_paragraphs(content)):
                body: Dict[str, Any] = {"to": chat_id, "text": chunk, "service": "iMessage"}
                if i == 0 and reply_to:
                    body["replyTo"] = reply_to
                if i == 0 and effect and effect in _VALID_EFFECTS:
                    body["effect"] = effect
                resp = await self._api_send(body)
                if resp is None:
                    return SendResult(success=False, error=f"Text delivery failed for {chat_id}")
        finally:
            await self._api_stop_typing(chat_id)

        message_id = (metadata or {}).get("message_id")
        if message_id and self._react_tapback:
            await self._api_remove_react(chat_id, message_id, self._react_tapback)
            if self._done_tapback and self._done_tapback in _VALID_TAPBACKS:
                await self._api_react(chat_id, message_id, self._done_tapback)

        return SendResult(success=True)

    # ==================================================================
    # PROXY API  (advanced-imessage-http-proxy)
    # https://github.com/photon-hq/advanced-imessage-http-proxy
    #
    # All responses follow: {"ok": true, "data": ...} envelope.
    # ==================================================================

    # ---- messaging (POST /send, POST /send/file, POST /send/sticker) --

    async def _api_send(self, body: Dict[str, Any]) -> Optional[Dict]:
        """POST /send — send text with optional effect/reply.

        The upstream may return HTTP 500 with "Message sent with an error"
        but still include a message ID.  We try to extract data even from
        error responses so callers can use the ID for edit/unsend/tapback.
        """
        if not self._http:
            return None
        try:
            resp = await self._http.post("/send", json=body)
            if not resp.is_success:
                logger.warning("iMessage POST /send HTTP %s: %s", resp.status_code, resp.text[:200])
            try:
                parsed = resp.json()
            except (json.JSONDecodeError, ValueError):
                return None
            if isinstance(parsed, dict):
                if "data" in parsed:
                    return parsed["data"]
                if parsed.get("id"):
                    return parsed
            return None
        except httpx.HTTPError as e:
            logger.warning("iMessage POST /send failed: %s", e, exc_info=True)
            return None

    async def _api_send_file(
        self, to: str, file_path: str, audio: bool = False,
    ) -> Optional[Dict]:
        """POST /send/file — send an image, document, or audio file.

        Uses multipart/form-data. May timeout on some Kit servers behind
        the centralized proxy (Cloudflare 60s limit).
        """
        if not self._http:
            return None
        try:
            mime, _ = mimetypes.guess_type(file_path)
            fname = Path(file_path).name
            data: Dict[str, Any] = {"to": to}
            if audio:
                data["audio"] = "true"
            with open(file_path, "rb") as f:
                resp = await self._http.post(
                    "/send/file",
                    data=data,
                    files={"file": (fname, f, mime or "application/octet-stream")},
                    timeout=120.0,
                )
            if not resp.is_success:
                logger.warning("iMessage POST /send/file HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            return self._unwrap(resp)
        except (httpx.HTTPError, OSError) as e:
            logger.warning("iMessage POST /send/file failed: %s", e, exc_info=True)
            return None

    # ---- tapbacks (POST/DELETE /messages/:id/react) --------------------

    async def _api_react(self, chat: str, message_id: str, tapback: str):
        """POST /messages/:id/react — add tapback (love, like, dislike, laugh, emphasize, question)."""
        if not self._http or not tapback:
            return
        try:
            await self._http.post(
                f"/messages/{message_id}/react",
                json={"chat": chat, "type": tapback},
            )
        except httpx.HTTPError:
            pass

    async def _api_remove_react(self, chat: str, message_id: str, tapback: str):
        """DELETE /messages/:id/react — remove tapback."""
        if not self._http or not tapback:
            return
        try:
            await self._http.request(
                "DELETE", f"/messages/{message_id}/react",
                json={"chat": chat, "type": tapback},
            )
        except httpx.HTTPError:
            pass

    # ---- messages (GET /messages, GET /messages/search, GET /messages/:id)

    async def _api_get_messages(
        self, limit: int = 50, chat: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /messages — query messages, optionally filtered by chat."""
        params: Dict[str, Any] = {"limit": limit}
        if chat:
            params["chat"] = chat
        data = await self._api_get("/messages", params=params)
        return data if isinstance(data, list) else []

    async def _api_search_messages(
        self, query: str, chat: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /messages/search — full-text search."""
        params: Dict[str, Any] = {"q": query}
        if chat:
            params["chat"] = chat
        data = await self._api_get("/messages/search", params=params)
        return data if isinstance(data, list) else []

    async def _api_get_message(self, message_id: str) -> Optional[Dict]:
        """GET /messages/:id — single message details."""
        data = await self._api_get(f"/messages/{message_id}")
        return data if isinstance(data, dict) else None

    # ---- chats (GET /chats, GET /chats/:id, mark-read, typing) --------

    async def _api_get_chats(self) -> List[Dict[str, Any]]:
        """GET /chats — list all conversations."""
        data = await self._api_get("/chats")
        return data if isinstance(data, list) else []

    async def _api_get_chat(self, address: str) -> Optional[Dict]:
        """GET /chats/:id — chat details."""
        data = await self._api_get(f"/chats/{address}")
        return data if isinstance(data, dict) else None

    async def _api_get_chat_messages(
        self, address: str, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /chats/:id/messages — message history for a chat."""
        data = await self._api_get(f"/chats/{address}/messages", params={"limit": limit})
        return data if isinstance(data, list) else []

    async def _api_get_chat_participants(self, address: str) -> List[Dict[str, Any]]:
        """GET /chats/:id/participants — group participants."""
        data = await self._api_get(f"/chats/{address}/participants")
        return data if isinstance(data, list) else []

    async def _api_mark_read(self, address: str):
        """POST /chats/:id/read — clear unread badge."""
        if not self._http:
            return
        try:
            await self._http.post(f"/chats/{address}/read")
        except httpx.HTTPError:
            pass

    async def _api_mark_unread(self, address: str):
        """POST /chats/:id/unread — mark as unread."""
        if not self._http:
            return
        try:
            await self._http.post(f"/chats/{address}/unread")
        except httpx.HTTPError:
            pass

    async def _api_start_typing(self, address: str):
        """POST /chats/:id/typing — show typing indicator."""
        if not self._http:
            return
        try:
            await self._http.post(f"/chats/{address}/typing")
            self._typing_chats.add(address)
        except httpx.HTTPError:
            pass

    async def _api_stop_typing(self, address: str):
        """DELETE /chats/:id/typing — stop typing indicator."""
        if not self._http:
            return
        try:
            await self._http.request("DELETE", f"/chats/{address}/typing")
        except httpx.HTTPError:
            pass
        self._typing_chats.discard(address)

    # ---- attachments ---------------------------------------------------

    async def _api_download_attachment(self, att_guid: str, filename: str) -> Optional[str]:
        """GET /attachments/:id — download to local cache."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(f"/attachments/{att_guid}")
            if not resp.is_success:
                return None
            from hermes_cli.config import get_hermes_home
            media_dir = get_hermes_home() / "image_cache"
            media_dir.mkdir(parents=True, exist_ok=True)
            sanitized_guid = att_guid.replace("/", "_").replace("\\", "_").replace("\x00", "")
            raw_name = Path(filename).name.replace("\x00", "") if filename else ""
            safe_name = f"{sanitized_guid}_{raw_name}" if raw_name else f"{sanitized_guid}.bin"
            dest = (media_dir / safe_name).resolve()
            if not dest.is_relative_to(media_dir.resolve()):
                dest = (media_dir / f"{sanitized_guid}.bin").resolve()
            dest.write_bytes(resp.content)
            return str(dest)
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Failed to download iMessage attachment %s: %s", att_guid, e, exc_info=True)
            return None

    async def _api_attachment_info(self, att_guid: str) -> Optional[Dict]:
        """GET /attachments/:id/info — file metadata (name, size, MIME type)."""
        data = await self._api_get(f"/attachments/{att_guid}/info")
        return data if isinstance(data, dict) else None

    # ---- contacts & handles --------------------------------------------

    async def _api_check_imessage(self, address: str) -> bool:
        """GET /check/:address — check if address uses iMessage."""
        data = await self._api_get(f"/check/{address}")
        if isinstance(data, dict):
            return bool(data.get("available") or data.get("imessage"))
        return False

    # ---- server --------------------------------------------------------

    async def _api_server_info(self) -> Optional[Dict]:
        """GET /server — server info."""
        data = await self._api_get("/server")
        return data if isinstance(data, dict) else None

    async def _api_health(self) -> bool:
        """GET /health — basic health check (no auth required)."""
        if not self._http:
            return False
        try:
            resp = await self._http.get("/health")
            if resp.is_success:
                logger.info("iMessage server health check passed")
                return True
            logger.warning("iMessage health check HTTP %s", resp.status_code)
        except httpx.HTTPError as e:
            logger.warning("iMessage health check failed: %s", e, exc_info=True)
        return False

    # ---- HTTP helpers --------------------------------------------------

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._http:
            return None
        try:
            resp = await self._http.get(path, params=params)
            return self._unwrap(resp)
        except httpx.HTTPError as e:
            logger.warning("iMessage GET %s failed: %s", path, e, exc_info=True)
            return None

    async def _api_post(self, path: str, body: Dict[str, Any]) -> Optional[Dict]:
        if not self._http:
            return None
        try:
            resp = await self._http.post(path, json=body)
            if not resp.is_success:
                logger.warning(
                    "iMessage POST %s HTTP %s: %s", path, resp.status_code, resp.text[:200]
                )
            return self._unwrap(resp)
        except httpx.HTTPError as e:
            logger.warning("iMessage POST %s failed: %s", path, e, exc_info=True)
            raise

    async def _api_delete(self, path: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        if not self._http:
            return None
        try:
            resp = await self._http.request("DELETE", path, json=body)
            return self._unwrap(resp)
        except httpx.HTTPError as e:
            logger.warning("iMessage DELETE %s failed: %s", path, e, exc_info=True)
            return None

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        """Unwrap the proxy's ``{"ok": true, "data": ...}`` envelope."""
        if not resp.is_success:
            return None
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # ---- dedup ---------------------------------------------------------

    def _is_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        return message_id in self._processed_ids

    def _mark_seen(self, message_id: str):
        if not message_id:
            return
        self._processed_ids[message_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)
