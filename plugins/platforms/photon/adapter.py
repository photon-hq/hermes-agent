"""
Photon Spectrum (iMessage) platform adapter for Hermes Agent.

Transport:
    A single supervised Node sidecar (see ``sidecar/index.mjs``) runs the
    ``spectrum-ts`` SDK and holds Photon's managed gRPC connection. There is
    no webhook and no public tunnel — the channel behaves like Telegram or
    Discord: one persistent connection for the lifetime of the gateway.

    Inbound:
        The sidecar consumes the SDK's ``app.messages`` gRPC stream and emits
        one newline-delimited JSON event per message on **stdout**. A reader
        task here parses each line, normalizes it to a ``MessageEvent`` and
        dispatches it via ``BasePlatformAdapter.handle_message``.

    Outbound:
        ``send`` / ``send_typing`` write a newline-delimited JSON command to
        the sidecar's **stdin** and await the correlated ack.

    The sidecar writes only NDJSON to stdout; all of its diagnostics go to
    stderr, which we pump into the Hermes logger. Closing the sidecar's stdin
    (on disconnect, or when the gateway exits) triggers a graceful shutdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from hermes_constants import get_hermes_home

from .auth import (
    _get_hermes_env_value,
    load_allowed_phone_numbers,
    load_project_credentials,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

# Photon iMessage messages from the SDK side have no documented hard limit,
# but the underlying iMessage protocol limits practical message size to
# ~16 KB.  Keep a conservative cap that matches BlueBubbles.
_MAX_MESSAGE_LENGTH = 8000

# Dedup parameters — the gRPC stream can redeliver the same message.id across
# reconnects, so keep a small in-memory window as cheap insurance.
_DEDUP_MAX_SIZE = 4000
_DEDUP_WINDOW_SECONDS = 48 * 3600

_SIDECAR_DIR = Path(__file__).parent / "sidecar"

# spectrum-ts establishes a gRPC connection on startup; give it room.
_READY_TIMEOUT_SECONDS = 20.0
_SEND_TIMEOUT_SECONDS = 30.0

# Protocol version the sidecar advertises in its ``ready`` event.
_SIDECAR_PROTOCOL = 1


# ---------------------------------------------------------------------------
# Module-level helpers — also used by check_fn / standalone send


def check_requirements() -> bool:
    """Return True when the Node sidecar can run (node + installed deps)."""
    if not shutil.which(os.getenv("PHOTON_NODE_BIN") or "node"):
        return False
    if not (_SIDECAR_DIR / "node_modules").exists():
        # spectrum-ts not installed yet — `hermes photon quick-setup` will
        # install it.  check_fn still returns False so the gateway surfaces
        # the missing-deps state in `hermes setup` / status.
        return False
    return True


def _sidecar_process_env(*, project_id: str, project_secret: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(get_hermes_home())
    env["PHOTON_PROJECT_ID"] = project_id
    env["PHOTON_PROJECT_SECRET"] = project_secret
    return env


def validate_config(cfg: PlatformConfig) -> bool:
    extra = cfg.extra or {}
    project_id = extra.get("project_id") or _get_hermes_env_value("PHOTON_PROJECT_ID")
    project_secret = (
        extra.get("project_secret") or _get_hermes_env_value("PHOTON_PROJECT_SECRET")
    )
    if not project_id or not project_secret:
        # Fall back to Hermes' .env loader; the process may not have
        # preloaded ~/.hermes/.env into os.environ yet.
        stored_id, stored_sec = load_project_credentials()
        return bool(stored_id and stored_sec)
    return True


def is_connected(cfg: PlatformConfig) -> bool:
    """Return True only when Photon can be enabled by the gateway.

    Parity with Telegram/Discord: valid project credentials + an installed
    sidecar + at least one authorized sender (or allow-all).
    """
    if not validate_config(cfg) or not check_requirements():
        return False
    extra = cfg.extra or {}
    if _truthy_photon_value(extra.get("allow_all")):
        return True
    if _truthy_photon_value(_get_hermes_env_value("PHOTON_ALLOW_ALL_USERS")):
        return True
    if _truthy_photon_value(_get_hermes_env_value("GATEWAY_ALLOW_ALL_USERS")):
        return True
    allowed = extra.get("allowed_users")
    if isinstance(allowed, str) and allowed.strip():
        return True
    if isinstance(allowed, (list, tuple, set)) and any(str(v).strip() for v in allowed):
        return True
    return bool(load_allowed_phone_numbers())


def _truthy_photon_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env so env-only setups appear in status."""
    project_id, project_secret = load_project_credentials()
    if not (project_id and project_secret):
        return None
    return {
        "project_id": project_id,
        "project_secret": project_secret,
    }


def _attachment_message_type(mime: str) -> MessageType:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return MessageType.PHOTO
    if mime.startswith("video/"):
        return MessageType.VIDEO
    if mime.startswith("audio/"):
        return MessageType.AUDIO
    return MessageType.DOCUMENT


# ---------------------------------------------------------------------------
# Adapter

class PhotonAdapter(BasePlatformAdapter):
    """Persistent spectrum-ts gRPC channel via a supervised Node sidecar."""

    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("photon"))
        extra = config.extra or {}

        # Project credentials (process env wins, then config.extra, then
        # Hermes' .env loader).
        stored_id, stored_sec = load_project_credentials()
        self._project_id: str = (
            os.getenv("PHOTON_PROJECT_ID")
            or extra.get("project_id")
            or stored_id
            or ""
        )
        self._project_secret: str = (
            os.getenv("PHOTON_PROJECT_SECRET")
            or extra.get("project_secret")
            or stored_sec
            or ""
        )

        self._autostart_sidecar = str(
            os.getenv("PHOTON_SIDECAR_AUTOSTART", "true")
        ).lower() not in ("0", "false", "no")
        self._node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node") or "node"

        # Runtime state
        self._sidecar_proc: Optional[subprocess.Popen] = None
        self._sidecar_stdin: Optional[Any] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._ready_event = asyncio.Event()
        self._ready_error: Optional[tuple[str, str]] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._inflight: set[asyncio.Task] = set()
        self._closing = False
        # Lightweight in-memory dedup keyed on message.id.
        self._seen_messages: Dict[str, float] = {}

    # -- Connection lifecycle ---------------------------------------------

    async def connect(self) -> bool:
        if not self._project_id or not self._project_secret:
            self._set_fatal_error(
                "MISSING_CREDENTIALS",
                "PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET are required. "
                "Run: hermes photon quick-setup --phone '<phone>'",
                retryable=False,
            )
            return False
        if not check_requirements():
            self._set_fatal_error(
                "MISSING_DEP",
                "Photon sidecar unavailable: install Node and run "
                f"`cd {_SIDECAR_DIR} && npm install` "
                "(or rerun `hermes photon quick-setup --phone '<phone>'`).",
                retryable=False,
            )
            return False
        if not self._autostart_sidecar:
            self._set_fatal_error(
                "SIDECAR_DISABLED",
                "PHOTON_SIDECAR_AUTOSTART is disabled — the sidecar is the "
                "only Photon transport, so the channel cannot run.",
                retryable=False,
            )
            return False

        # One gateway per Spectrum project: two streams on the same project
        # would double-deliver and double-reply.
        if not self._acquire_platform_lock(
            "photon", self._project_id, "Photon Spectrum project"
        ):
            return False

        try:
            await self._start_sidecar()
        except Exception as e:
            self._release_platform_lock()
            self._set_fatal_error(
                "SIDECAR_FAILED",
                f"failed to start Photon sidecar: {e}",
                retryable=True,
            )
            await self._stop_sidecar()
            return False

        self._mark_connected()
        logger.info(
            "[photon] connected via spectrum-ts gRPC stream (project %s, home %s)",
            self._project_id,
            get_hermes_home(),
        )
        return True

    async def disconnect(self) -> None:
        self._closing = True
        await self._stop_sidecar()
        self._fail_pending("Photon adapter disconnected")
        self._release_platform_lock()
        self._mark_disconnected()

    # -- Sidecar lifecycle -------------------------------------------------

    async def _start_sidecar(self) -> None:
        if not (_SIDECAR_DIR / "node_modules").exists():
            raise RuntimeError(
                f"Photon sidecar deps not installed. Run: "
                f"cd {_SIDECAR_DIR} && npm install   "
                "(or rerun `hermes photon quick-setup --phone '<phone>'`)"
            )

        self._closing = False
        self._ready_event = asyncio.Event()
        self._ready_error = None

        env = _sidecar_process_env(
            project_id=self._project_id,
            project_secret=self._project_secret,
        )
        self._sidecar_proc = subprocess.Popen(  # noqa: S603
            [self._node_bin, str(_SIDECAR_DIR / "index.mjs")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=(sys.platform != "win32"),
        )
        self._sidecar_stdin = self._sidecar_proc.stdin

        loop = asyncio.get_event_loop()
        self._reader_task = loop.create_task(
            self._read_sidecar_events(self._sidecar_proc.stdout)
        )
        self._stderr_task = loop.create_task(
            self._pump_sidecar_stderr(self._sidecar_proc.stderr)
        )

        try:
            await asyncio.wait_for(
                self._ready_event.wait(), timeout=_READY_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Photon sidecar did not become ready within "
                f"{int(_READY_TIMEOUT_SECONDS)}s"
            )
        if self._ready_error is not None:
            code, message = self._ready_error
            raise RuntimeError(f"Photon sidecar failed to start ({code}): {message}")

    async def _read_sidecar_events(self, stdout: Any) -> None:
        """Parse the sidecar's NDJSON stdout and dispatch events."""
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, stdout.readline)
            if not line:
                break  # EOF — sidecar exited
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("[photon-sidecar] non-JSON stdout line: %s", text[:200])
                continue
            if not isinstance(event, dict):
                continue
            try:
                self._handle_sidecar_event(event)
            except Exception:
                logger.exception("[photon] failed handling sidecar event")

        # stdout closed — the sidecar process has gone away.
        if self._ready_error is None and not self._ready_event.is_set():
            self._ready_error = ("SIDECAR_EXITED", "sidecar exited before ready")
            self._ready_event.set()
        if not self._closing:
            logger.error("[photon] sidecar stream closed unexpectedly")
            self._fail_pending("Photon sidecar exited")
            self._set_fatal_error(
                "SIDECAR_EXITED",
                "Photon sidecar exited unexpectedly",
                retryable=True,
            )
            await self._notify_fatal_error()

    def _handle_sidecar_event(self, event: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "ready":
            if event.get("protocol") != _SIDECAR_PROTOCOL:
                logger.warning(
                    "[photon] sidecar protocol %s != expected %s",
                    event.get("protocol"), _SIDECAR_PROTOCOL,
                )
            self._ready_event.set()
            return
        if etype == "fatal":
            self._ready_error = (
                str(event.get("code") or "FATAL"),
                str(event.get("error") or "sidecar reported a fatal error"),
            )
            self._ready_event.set()
            return
        if etype == "message":
            # Fire-and-forget: the reader must NOT await dispatch. handle_message
            # may trigger an outbound send whose ack only this reader can
            # deliver — awaiting inline would deadlock. Track the task so it is
            # not garbage-collected while pending.
            task = asyncio.ensure_future(self._dispatch_event(event))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            return
        if etype in ("sent", "error"):
            self._resolve_pending(event)
            return
        logger.debug("[photon] ignoring sidecar event type %r", etype)

    def _resolve_pending(self, event: Dict[str, Any]) -> None:
        cid = event.get("cid")
        if not cid:
            if event.get("type") == "error":
                logger.warning(
                    "[photon-sidecar] stream error: %s", event.get("error")
                )
            return
        future = self._pending.pop(cid, None)
        if future is None or future.done():
            return
        if event.get("type") == "sent" and event.get("ok"):
            future.set_result(event)
        else:
            future.set_exception(
                RuntimeError(event.get("error") or "sidecar reported failure")
            )

    def _fail_pending(self, reason: str) -> None:
        for cid, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
            self._pending.pop(cid, None)

    async def _pump_sidecar_stderr(self, stderr: Any) -> None:
        """Pump the sidecar's stderr (its only log channel) into our logger."""
        if stderr is None:
            return
        loop = asyncio.get_event_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, stderr.readline)
                if not line:
                    break
                logger.info(
                    "[photon-sidecar] %s", line.decode("utf-8", "replace").rstrip()
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[photon-sidecar] stderr pump exited: %s", e)

    async def _stop_sidecar(self) -> None:
        proc = self._sidecar_proc
        # Closing stdin signals the sidecar to shut down gracefully (EOF).
        if self._sidecar_stdin is not None:
            try:
                await self._write_command_line({"type": "shutdown"})
            except Exception:
                pass
            try:
                self._sidecar_stdin.close()
            except Exception:
                pass
            self._sidecar_stdin = None
        if proc is not None:
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._sidecar_proc = None
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

    # -- Inbound -----------------------------------------------------------

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        msg_id = event.get("id")
        if msg_id and self._is_duplicate(str(msg_id)):
            logger.info("[photon] duplicate inbound ignored: message_id=%s", msg_id)
            return
        message_event = self._event_to_message_event(event)
        if message_event is None:
            return
        try:
            await self.handle_message(message_event)
        except Exception:
            logger.exception("[photon] inbound dispatch failed")

    def _event_to_message_event(self, event: Dict[str, Any]) -> Optional[MessageEvent]:
        """Normalize a sidecar ``message`` event into a MessageEvent.

        Pure/synchronous so it is unit-testable without a running sidecar or
        event loop.
        """
        space_id = event.get("spaceId") or ""
        if not space_id:
            logger.warning("[photon] inbound missing spaceId")
            return None
        sender_id = event.get("sender") or ""
        content = event.get("content") or {}

        # Photon documents iMessage DM ids as `any;-;+E164` and group ids as
        # `any;+;<chat-guid>`. Use the group marker as the heuristic.
        chat_type = "group" if ";+;" in space_id else "dm"

        ts_str = event.get("timestamp") or ""
        try:
            timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except ValueError:
            timestamp = datetime.now(tz=timezone.utc)

        if content.get("type") == "text":
            text = content.get("text") or ""
            mtype = MessageType.TEXT
        elif content.get("type") == "attachment":
            name = content.get("name") or "(unnamed)"
            mime = content.get("mimeType") or ""
            text = f"[Photon attachment received: {name} ({mime}) — no download URL yet]"
            mtype = _attachment_message_type(mime)
        else:
            text = f"[Photon content type not handled: {content.get('type')}]"
            mtype = MessageType.TEXT

        source = self.build_source(
            chat_id=space_id,
            chat_name=space_id,
            chat_type=chat_type,
            user_id=sender_id or space_id,
            user_name=sender_id or None,
        )
        return MessageEvent(
            text=text,
            message_type=mtype,
            source=source,
            message_id=event.get("id"),
            raw_message=event,
            timestamp=timestamp,
        )

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > _DEDUP_MAX_SIZE:
            cutoff = now - _DEDUP_WINDOW_SECONDS
            self._seen_messages = {
                k: v for k, v in self._seen_messages.items() if v > cutoff
            }
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # -- Outbound ----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if len(content) > self.MAX_MESSAGE_LENGTH:
            logger.warning(
                "[photon] truncating outbound from %d to %d chars",
                len(content), self.MAX_MESSAGE_LENGTH,
            )
            content = content[: self.MAX_MESSAGE_LENGTH]
        try:
            data = await self._send_command(
                "send", spaceId=chat_id, text=content, replyTo=reply_to
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        return SendResult(success=True, message_id=data.get("messageId"))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self._send_command("typing", spaceId=chat_id)
        except Exception as e:
            logger.debug("[photon] send_typing failed: %s", e)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return whatever we know about a Spectrum space id.

        Photon's `space.id` is opaque (`any;-;+E164` for DMs,
        `any;+;<guid>` for groups). We surface that shape directly so the
        gateway has something to show in session pickers / logs.
        """
        chat_type = "group" if ";+;" in chat_id else "dm"
        return {"name": chat_id, "type": chat_type, "id": chat_id}

    async def _send_command(self, command: str, **fields: Any) -> Dict[str, Any]:
        if self._sidecar_stdin is None or self._sidecar_proc is None:
            raise RuntimeError("Photon sidecar not running")
        cid = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[cid] = future
        payload = {"type": command, "cid": cid, **fields}
        try:
            await self._write_command_line(payload)
            return await asyncio.wait_for(future, timeout=_SEND_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(cid, None)

    async def _write_command_line(self, payload: Dict[str, Any]) -> None:
        stdin = self._sidecar_stdin
        if stdin is None:
            raise RuntimeError("Photon sidecar stdin closed")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        loop = asyncio.get_event_loop()

        def _write() -> None:
            stdin.write(data)
            stdin.flush()

        await loop.run_in_executor(None, _write)


# ---------------------------------------------------------------------------
# Standalone (out-of-process) send for cron deliveries when the gateway is not
# co-resident.  Spawns a one-shot sidecar, waits for ready, sends one message,
# then closes stdin so the sidecar shuts down.

async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,  # noqa: ARG001 — Spectrum has no threads yet
    media_files: Optional[list] = None,  # noqa: ARG001 — attachment send not supported yet
    force_document: bool = False,  # noqa: ARG001
) -> Dict[str, Any]:
    extra = pconfig.extra or {}
    stored_id, stored_sec = load_project_credentials()
    project_id = (
        os.getenv("PHOTON_PROJECT_ID") or extra.get("project_id") or stored_id or ""
    )
    project_secret = (
        os.getenv("PHOTON_PROJECT_SECRET")
        or extra.get("project_secret")
        or stored_sec
        or ""
    )
    if not (project_id and project_secret):
        return {"error": "PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET are required"}
    if not check_requirements():
        return {"error": "Photon sidecar unavailable (install Node + run npm install)"}

    node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node") or "node"
    env = _sidecar_process_env(project_id=project_id, project_secret=project_secret)
    try:
        proc = await asyncio.create_subprocess_exec(
            node_bin,
            str(_SIDECAR_DIR / "index.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        return {"error": f"Photon sidecar spawn failed: {e}"}

    async def _read_until(*, cid: Optional[str], want_ready: bool) -> Dict[str, Any]:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise RuntimeError("sidecar exited before responding")
            try:
                event = json.loads(raw.decode("utf-8", "replace").strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if want_ready and etype == "ready":
                return event
            if etype == "fatal":
                raise RuntimeError(event.get("error") or "sidecar fatal error")
            if cid and etype in ("sent", "error") and event.get("cid") == cid:
                return event

    try:
        await asyncio.wait_for(
            _read_until(cid=None, want_ready=True), timeout=_READY_TIMEOUT_SECONDS
        )
        cid = uuid.uuid4().hex
        cmd = {
            "type": "send",
            "cid": cid,
            "spaceId": chat_id,
            "text": message[:_MAX_MESSAGE_LENGTH],
        }
        proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        result = await asyncio.wait_for(
            _read_until(cid=cid, want_ready=False), timeout=_SEND_TIMEOUT_SECONDS
        )
        if result.get("type") == "sent" and result.get("ok"):
            return {"success": True, "message_id": result.get("messageId")}
        return {"error": result.get("error") or "sidecar reported failure"}
    except Exception as e:
        return {"error": f"Photon standalone send failed: {e}"}
    finally:
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin entry point

def register(ctx) -> None:
    """Called by the Hermes plugin loader at startup."""
    from . import cli as _cli  # local import to avoid argparse at module load

    ctx.register_platform(
        name="photon",
        label="iMessage (via Photon)",
        adapter_factory=lambda cfg: PhotonAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"],
        install_hint=(
            "Run `hermes photon login`, then `hermes photon quick-setup "
            "--phone '+<country-code><number>'` to create/adopt a Spectrum "
            "project, link your phone number, and install the sidecar that "
            "streams iMessage over Photon's gRPC gateway."
        ),
        setup_fn=_cli.interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="PHOTON_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="PHOTON_ALLOWED_USERS",
        allow_all_env="PHOTON_ALLOW_ALL_USERS",
        max_message_length=_MAX_MESSAGE_LENGTH,
        emoji="📱",
        # iMessage carries E.164 phone numbers — treat session descriptions
        # as PII-sensitive so they get redacted in logs.
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via Photon Spectrum (iMessage). "
            "Treat replies like regular text messages — short, friendly, no "
            "markdown rendering. Recipient identifiers are E.164 phone "
            "numbers; never expose them in responses unless the user asked. "
            "Attachments arrive as metadata only (no download URL yet)."
        ),
    )

    ctx.register_cli_command(
        name="photon",
        help="Set up and manage the Photon iMessage integration",
        setup_fn=_cli.register_cli,
        handler_fn=_cli.dispatch,
    )
