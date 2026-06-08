"""
Photon Spectrum (iMessage) platform adapter for Hermes Agent.

The primary Photon runtime boundary is this Python adapter. It owns the Hermes
contract directly:

- Spectrum SDK event normalization
- ``MessageEvent`` creation
- outbound payload construction
- ``SendResult`` mapping
- adapter health/status and current-home runtime state

The Spectrum SDK is TypeScript-only today, so the adapter starts a private Node
sidecar over stdio. That sidecar is an implementation detail of ``adapter.py``
and does not expose a separate runtime surface.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
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
    merge_pending_message_event,
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

_MAX_MESSAGE_LENGTH = 8000
_DEDUP_MAX_SIZE = 4000
_DEDUP_WINDOW_SECONDS = 48 * 3600

# The private Node sidecar contains the Spectrum SDK dependency tree.
_SIDECAR_DIR = Path(__file__).parent / "sidecar"

_SIDECAR_READY_TIMEOUT_SECONDS = 20.0
_SIDECAR_REQUEST_TIMEOUT_SECONDS = 30.0
_SIDECAR_ATTACHMENT_TIMEOUT_SECONDS = 120.0
_SIDECAR_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_INBOUND_MEDIA_BATCH_DELAY_SECONDS = 1.0
_MEDIA_FOLLOWUP_TYPES = {
    MessageType.PHOTO,
    MessageType.VIDEO,
    MessageType.AUDIO,
    MessageType.VOICE,
    MessageType.DOCUMENT,
    MessageType.STICKER,
}
_PHOTON_MEDIA_DIRECTIVE_EXTS = (
    "png|jpe?g|gif|webp|heic|heif|bmp|tiff?|svg|"
    "mp4|mov|avi|mkv|webm|3gp|"
    "ogg|opus|mp3|wav|m4a|flac|"
    "epub|pdf|zip|rar|7z|tar|gz|tgz|bz2|xz|"
    "docx?|xlsx?|xls|pptx?|ppt|txt|md|csv|tsv|json|xml|ya?ml|html?|rtf|odt|ods|key|apk|ipa"
)
_PHOTON_MEDIA_DIRECTIVE_RE = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/)\S+(?:[^\S\n]+\S+)*?\.(?:'''
    + _PHOTON_MEDIA_DIRECTIVE_EXTS
    + r''')(?=[\s`"',;:)\]}]|$))[`"']?''',
    re.IGNORECASE,
)


_SIDECAR_ENTRYPOINT = _SIDECAR_DIR / "index.mjs"


# ---------------------------------------------------------------------------
# Module-level helpers


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configured_float(
    value: Any,
    default: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _extract_photon_media_directives(content: str) -> tuple[list[tuple[str, bool]], str]:
    media: list[tuple[str, bool]] = []
    has_voice_tag = "[[audio_as_voice]]" in content

    for match in _PHOTON_MEDIA_DIRECTIVE_RE.finditer(content):
        path = match.group("path").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
            path = path[1:-1].strip()
        path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
        if path:
            media.append((os.path.expanduser(path), has_voice_tag))

    cleaned = content
    if media:
        cleaned = _PHOTON_MEDIA_DIRECTIVE_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return media, cleaned


def adapter_runtime_state_path() -> Path:
    return Path(get_hermes_home()) / "photon" / "adapter-runtime.json"


def read_adapter_runtime_state() -> Dict[str, Any]:
    try:
        return json.loads(adapter_runtime_state_path().read_text())
    except Exception:
        return {}


def check_requirements() -> bool:
    """Return True when the private Spectrum SDK sidecar can be started."""
    if not shutil.which(os.getenv("PHOTON_NODE_BIN") or "node"):
        return False
    if not (_SIDECAR_DIR / "node_modules").exists():
        # spectrum-ts not installed yet. `hermes photon setup` installs it.
        return False
    return True


def _adapter_process_env(*, project_id: str, project_secret: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(get_hermes_home())
    env["PHOTON_PROJECT_ID"] = project_id
    env["PHOTON_PROJECT_SECRET"] = project_secret
    return env


def _configured_project_credentials(pconfig: Any = None) -> tuple[str, str]:
    extra = getattr(pconfig, "extra", {}) or {}
    stored_id, stored_secret = load_project_credentials()
    project_id = (
        os.getenv("PHOTON_PROJECT_ID")
        or extra.get("project_id")
        or stored_id
        or ""
    )
    project_secret = (
        os.getenv("PHOTON_PROJECT_SECRET")
        or extra.get("project_secret")
        or stored_secret
        or ""
    )
    return str(project_id).strip(), str(project_secret).strip()


def validate_config(cfg: PlatformConfig) -> bool:
    extra = cfg.extra or {}
    project_id = extra.get("project_id") or _get_hermes_env_value("PHOTON_PROJECT_ID")
    project_secret = (
        extra.get("project_secret") or _get_hermes_env_value("PHOTON_PROJECT_SECRET")
    )
    if not project_id or not project_secret:
        stored_id, stored_sec = load_project_credentials()
        return bool(stored_id and stored_sec)
    return True


def is_connected(cfg: PlatformConfig) -> bool:
    """Return True only when Photon can be enabled by the gateway."""
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
    """Seed PlatformConfig.extra and home channel from env for gateway status."""
    project_id, project_secret = load_project_credentials()
    if not (project_id and project_secret):
        return None
    seed: dict[str, Any] = {
        "project_id": project_id,
        "project_secret": project_secret,
    }
    home = (_get_hermes_env_value("PHOTON_HOME_CHANNEL") or "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": (
                (_get_hermes_env_value("PHOTON_HOME_CHANNEL_NAME") or "").strip()
                or "You (iMessage)"
            ),
        }
    return seed


# Structured adapter errors


class PhotonAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AdapterUnavailableError(PhotonAdapterError):
    def __init__(self, message: str = "Photon Spectrum adapter is not running"):
        super().__init__("ADAPTER_UNAVAILABLE", message, retryable=True)


class BadAdapterResponseError(PhotonAdapterError):
    def __init__(self, message: str):
        super().__init__("BAD_ADAPTER_RESPONSE", message, retryable=True)


class RetryableAdapterError(PhotonAdapterError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=True)


class NonRetryableAdapterError(PhotonAdapterError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=False)


class PermanentAdapterError(PhotonAdapterError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=False)


# ---------------------------------------------------------------------------
# Adapter


class PhotonAdapter(BasePlatformAdapter):
    """Photon adapter backed by a private Spectrum SDK sidecar."""

    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("photon"))
        extra = config.extra or {}

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
        self._project_name: str = (
            os.getenv("PHOTON_PROJECT_NAME")
            or extra.get("project_name")
            or "hermes-agent"
        )
        self._operator_phone: str = (
            os.getenv("PHOTON_OPERATOR_PHONE")
            or extra.get("operator_phone")
            or ""
        )
        self._node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node") or "node"

        self._sidecar_proc: Optional[asyncio.subprocess.Process] = None
        self._sidecar_stdout_task: Optional[asyncio.Task] = None
        self._sidecar_stderr_task: Optional[asyncio.Task] = None
        self._sidecar_wait_task: Optional[asyncio.Task] = None
        self._sidecar_ready: Optional[asyncio.Future] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._sdk_request_lock = asyncio.Lock()
        self._media_batch_delay_seconds = _configured_float(
            os.getenv("PHOTON_MEDIA_BATCH_DELAY_SECONDS")
            or extra.get("media_batch_delay_seconds"),
            _INBOUND_MEDIA_BATCH_DELAY_SECONDS,
            min_value=0.0,
            max_value=5.0,
        )
        self._pending_media_batches: Dict[str, MessageEvent] = {}
        self._pending_media_batch_tasks: Dict[str, asyncio.Task] = {}
        self._seen_messages: Dict[str, float] = {}
        self._stopping = False

        self._adapter_state = "disconnected"
        self._sdk_connected = False
        self._started_at: Optional[str] = None
        self._last_event_at: Optional[str] = None
        self._last_send_at: Optional[str] = None
        self._last_error: Optional[Dict[str, Any]] = None

    async def handle_message(self, event: MessageEvent) -> None:
        """Queue active Photon media follow-ups before the global busy handler."""
        if self._is_active_media_followup_event(event):
            session_key = self._media_batch_session_key(event)
            if session_key in self._active_sessions:
                self._heal_stale_session_lock(session_key)
            if session_key in self._active_sessions:
                logger.debug(
                    "[photon] queueing active media follow-up for session %s without interrupt",
                    session_key,
                )
                merge_pending_message_event(
                    self._pending_messages,
                    session_key,
                    event,
                    merge_text=event.message_type == MessageType.TEXT,
                )
                return
        await super().handle_message(event)

    def _is_active_media_followup_event(self, event: MessageEvent) -> bool:
        if event.is_command():
            return False
        return bool(event.media_urls or event.message_type in _MEDIA_FOLLOWUP_TYPES)

    # -- Connection lifecycle ---------------------------------------------

    async def connect(self) -> bool:
        if not self._project_id or not self._project_secret:
            self._set_fatal_error(
                "MISSING_CREDENTIALS",
                "PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET are required. "
                "Run: hermes photon setup '<phone>'",
                retryable=False,
            )
            self._adapter_state = "fatal"
            self._write_adapter_runtime_state()
            return False
        if not shutil.which(self._node_bin):
            self._set_fatal_error(
                "MISSING_NODE",
                "Node.js is required for the Photon Spectrum SDK sidecar",
                retryable=False,
            )
            self._adapter_state = "fatal"
            self._write_adapter_runtime_state()
            return False
        if not self._acquire_platform_lock(
            "photon",
            self._project_id,
            "Photon Spectrum project",
        ):
            self._adapter_state = "fatal"
            self._last_error = {
                "code": self.fatal_error_code or "photon_lock",
                "message": self.fatal_error_message
                or "Photon Spectrum project already in use",
                "retryable": False,
                "at": _utc_now_iso(),
            }
            self._write_adapter_runtime_state()
            return False
        try:
            await self._start_sdk_sidecar()
        except Exception as e:
            await self._stop_sdk_sidecar()
            self._release_platform_lock()
            retryable = not isinstance(e, PermanentAdapterError)
            self._set_fatal_error(
                getattr(e, "code", "SDK_SIDECAR_FAILED"),
                f"failed to start Photon Spectrum adapter: {e}",
                retryable=retryable,
            )
            self._adapter_state = "fatal"
            self._write_adapter_runtime_state()
            return False

        self._adapter_state = "connected"
        self._mark_connected()
        self._write_adapter_runtime_state()
        logger.info(
            "[photon] connected via private Spectrum SDK sidecar pid=%s project=%s",
            self._sidecar_proc.pid if self._sidecar_proc else "-",
            self._project_id,
        )
        return True

    async def disconnect(self) -> None:
        self._stopping = True
        self._cancel_media_batch_tasks()
        await self._stop_sdk_sidecar()
        self._release_platform_lock()
        self._adapter_state = "disconnected"
        self._sdk_connected = False
        self._mark_disconnected()
        self._write_adapter_runtime_state()

    # -- Private Spectrum SDK sidecar --------------------------------------

    async def _start_sdk_sidecar(self) -> None:
        if not (_SIDECAR_DIR / "node_modules").exists():
            raise PermanentAdapterError(
                "MISSING_SPECTRUM_SDK",
                f"Photon sidecar deps are not installed. Run: cd {_SIDECAR_DIR} && npm install",
            )

        self._stopping = False
        env = _adapter_process_env(
            project_id=self._project_id,
            project_secret=self._project_secret,
        )
        proc = await asyncio.create_subprocess_exec(
            self._node_bin,
            str(_SIDECAR_ENTRYPOINT),
            cwd=str(_SIDECAR_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=(sys.platform != "win32"),
        )
        self._sidecar_proc = proc
        self._sidecar_ready = asyncio.get_running_loop().create_future()
        self._sidecar_stdout_task = asyncio.create_task(self._read_sidecar_stdout(proc))
        self._sidecar_stderr_task = asyncio.create_task(self._read_sidecar_stderr(proc))
        self._sidecar_wait_task = asyncio.create_task(self._watch_sidecar_exit(proc))

        try:
            await asyncio.wait_for(
                self._sidecar_ready,
                timeout=_SIDECAR_READY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise RetryableAdapterError(
                "SDK_SIDECAR_TIMEOUT",
                "Spectrum SDK sidecar did not become ready in time",
            ) from e

    async def _read_sidecar_stdout(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[photon-adapter] non-json sidecar output: %s", line)
                    continue
                await self._handle_sidecar_message(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive supervision
            logger.warning("[photon-adapter] stdout reader failed: %s", e)

    async def _read_sidecar_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                logger.info(
                    "[photon-adapter] %s",
                    raw.decode("utf-8", "replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive supervision
            logger.debug("[photon-adapter] stderr reader failed: %s", e)

    async def _watch_sidecar_exit(self, proc: asyncio.subprocess.Process) -> None:
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive supervision
            logger.warning("[photon-adapter] sidecar wait failed: %s", e)
            return

        if proc is not self._sidecar_proc:
            return
        self._sidecar_proc = None
        self._sdk_connected = False
        self._adapter_state = "disconnected" if self._stopping else "failed"
        self._mark_disconnected()
        self._fail_pending_requests(
            RetryableAdapterError(
                "SDK_SIDECAR_EXITED",
                f"Spectrum SDK sidecar exited with code {code}",
            )
        )
        if self._sidecar_ready is not None and not self._sidecar_ready.done():
            self._sidecar_ready.set_exception(
                RetryableAdapterError(
                    "SDK_SIDECAR_EXITED",
                    f"Spectrum SDK sidecar exited with code {code}",
                )
            )
        self._last_error = {
            "code": "SDK_SIDECAR_EXITED",
            "message": f"Spectrum SDK sidecar exited with code {code}",
            "retryable": True,
            "at": _utc_now_iso(),
        }
        self._write_adapter_runtime_state()

    async def _handle_sidecar_message(self, payload: Dict[str, Any]) -> None:
        message_type = payload.get("type")
        if message_type == "ready":
            self._sdk_connected = True
            self._adapter_state = "connected"
            self._started_at = str(payload.get("startedAt") or _utc_now_iso())
            self._last_error = None
            if self._sidecar_ready is not None and not self._sidecar_ready.done():
                self._sidecar_ready.set_result(True)
            self._write_adapter_runtime_state()
            return

        if message_type == "event":
            await self._handle_sdk_event(payload.get("event") or {})
            return

        if message_type == "response":
            self._resolve_sidecar_response(payload)
            return

        if message_type == "log":
            level = str(payload.get("level") or "info").lower()
            log = logger.debug if level == "debug" else logger.info
            log("[photon-adapter] %s", payload.get("message") or "")
            return

        if message_type in {"error", "fatal", "stream_error"}:
            error = _adapter_error_from_payload(payload.get("error") or {})
            self._last_error = {
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
                "at": _utc_now_iso(),
            }
            if message_type == "fatal":
                self._adapter_state = "fatal"
                self._sdk_connected = False
                self._set_fatal_error(error.code, str(error), retryable=error.retryable)
                self._fail_pending_requests(error)
                if self._sidecar_ready is not None and not self._sidecar_ready.done():
                    self._sidecar_ready.set_exception(error)
            elif message_type == "stream_error":
                self._adapter_state = "failed"
                self._sdk_connected = False
                self._mark_disconnected()
                self._fail_pending_requests(error)
            self._write_adapter_runtime_state()
            return

        logger.debug("[photon-adapter] ignoring unknown sidecar payload: %s", payload)

    def _resolve_sidecar_response(self, payload: Dict[str, Any]) -> None:
        request_id = str(payload.get("requestId") or "")
        future = self._pending_requests.pop(request_id, None)
        if future is None or future.done():
            return
        if payload.get("ok"):
            data = payload.get("data")
            if not isinstance(data, dict):
                future.set_exception(
                    BadAdapterResponseError("sidecar response data was not an object")
                )
                return
            future.set_result(data)
            return
        future.set_exception(_adapter_error_from_payload(payload.get("error") or {}))

    async def _stop_sdk_sidecar(self) -> None:
        proc = self._sidecar_proc
        if proc is None:
            self._fail_pending_requests(AdapterUnavailableError())
            return

        try:
            if proc.returncode is None:
                try:
                    await self._sdk_request(
                        "shutdown",
                        {},
                        timeout=_SIDECAR_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=_SIDECAR_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    self._terminate_sidecar(proc)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
        finally:
            self._sidecar_proc = None
            for task in (
                self._sidecar_stdout_task,
                self._sidecar_stderr_task,
                self._sidecar_wait_task,
            ):
                if task is not None:
                    task.cancel()
            self._sidecar_stdout_task = None
            self._sidecar_stderr_task = None
            self._sidecar_wait_task = None
            self._sdk_connected = False
            self._fail_pending_requests(AdapterUnavailableError("Photon adapter stopped"))

    async def _force_stop_sdk_sidecar(self, reason: str) -> None:
        proc = self._sidecar_proc
        self._sidecar_proc = None
        self._sdk_connected = False

        if proc is not None and proc.returncode is None:
            self._terminate_sidecar(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except Exception:
                logger.debug("[photon] forced sidecar stop failed during %s", reason, exc_info=True)

        for task in (
            self._sidecar_stdout_task,
            self._sidecar_stderr_task,
            self._sidecar_wait_task,
        ):
            if task is not None:
                task.cancel()
        self._sidecar_stdout_task = None
        self._sidecar_stderr_task = None
        self._sidecar_wait_task = None
        self._sidecar_ready = None
        self._fail_pending_requests(
            AdapterUnavailableError(f"Photon adapter restarted after {reason}")
        )

    async def _restart_sdk_sidecar_after_timeout(self, command_type: str) -> None:
        logger.warning(
            "[photon] sidecar command %s timed out; recycling Spectrum sidecar",
            command_type,
        )
        self._adapter_state = "restarting"
        self._sdk_connected = False
        self._mark_disconnected()
        self._write_adapter_runtime_state()
        await self._force_stop_sdk_sidecar(f"{command_type} timeout")
        if self._stopping:
            return
        try:
            await self._start_sdk_sidecar()
        except Exception as e:
            self._adapter_state = "failed"
            self._sdk_connected = False
            self._last_error = {
                "code": getattr(e, "code", "SDK_SIDECAR_RESTART_FAILED"),
                "message": f"failed to restart Photon Spectrum adapter: {e}",
                "retryable": not isinstance(e, PermanentAdapterError),
                "at": _utc_now_iso(),
            }
            self._mark_disconnected()
            self._write_adapter_runtime_state()
            logger.warning("[photon] sidecar restart failed after timeout: %s", e)
            return

        self._adapter_state = "connected"
        self._sdk_connected = True
        self._last_error = None
        self._mark_connected()
        self._write_adapter_runtime_state()
        logger.info(
            "[photon] Spectrum sidecar recycled after %s timeout; pid=%s",
            command_type,
            self._sidecar_proc.pid if self._sidecar_proc else "-",
        )

    def _terminate_sidecar(self, proc: asyncio.subprocess.Process) -> None:
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return
            except (ProcessLookupError, PermissionError):
                pass
        proc.terminate()

    def _fail_pending_requests(self, error: PhotonAdapterError) -> None:
        for future in list(self._pending_requests.values()):
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()

    async def _sdk_request(
        self,
        command_type: str,
        payload: Dict[str, Any],
        *,
        timeout: float = _SIDECAR_REQUEST_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        async with self._sdk_request_lock:
            proc = self._sidecar_proc
            if proc is None or proc.returncode is not None or proc.stdin is None:
                raise AdapterUnavailableError()
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self._pending_requests[request_id] = future
            body = {"requestId": request_id, "type": command_type, **payload}
            try:
                proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            except Exception as e:
                self._pending_requests.pop(request_id, None)
                raise AdapterUnavailableError(f"could not write to Photon adapter: {e}") from e

            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as e:
                self._pending_requests.pop(request_id, None)
                if command_type in {"send", "send_attachment"}:
                    self._last_error = {
                        "code": "SDK_REQUEST_TIMEOUT",
                        "message": f"Photon adapter command {command_type} timed out",
                        "retryable": False,
                        "at": _utc_now_iso(),
                    }
                    self._write_adapter_runtime_state()
                    await self._restart_sdk_sidecar_after_timeout(command_type)
                    raise NonRetryableAdapterError(
                        "SDK_REQUEST_TIMEOUT",
                        f"Photon adapter command {command_type} timed out",
                    ) from e
                raise RetryableAdapterError(
                    "SDK_REQUEST_TIMEOUT",
                    f"Photon adapter command {command_type} timed out",
                ) from e

    # -- Inbound -----------------------------------------------------------

    async def _handle_sdk_event(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            logger.warning("[photon] ignored malformed SDK event: %r", payload)
            return
        message_id = _extract_message_id(payload)
        if not message_id:
            message_id = _stable_event_id(payload)
        if self._is_duplicate(message_id):
            logger.info("[photon] duplicate SDK message ignored: message_id=%s", message_id)
            return
        try:
            event = self._message_event_from_sdk_event(payload, message_id=message_id)
        except ValueError as e:
            logger.warning("[photon] ignored SDK event: %s", e)
            return
        self._last_event_at = _utc_now_iso()
        self._write_adapter_runtime_state()
        if self._should_batch_inbound_text_event(event):
            await self._merge_text_into_media_batch(event)
            return
        if self._should_batch_inbound_media_event(event):
            await self._enqueue_media_event(event)
            return
        await self.handle_message(event)

    def _cancel_media_batch_tasks(self) -> None:
        for task in self._pending_media_batch_tasks.values():
            if task is not None and not task.done():
                task.cancel()
        self._pending_media_batch_tasks.clear()
        self._pending_media_batches.clear()

    def _should_batch_inbound_media_event(self, event: MessageEvent) -> bool:
        return bool(
            self._media_batch_delay_seconds > 0
            and event.media_urls
            and event.message_type
            in {
                MessageType.PHOTO,
                MessageType.VIDEO,
                MessageType.AUDIO,
                MessageType.VOICE,
                MessageType.DOCUMENT,
            }
        )

    def _should_batch_inbound_text_event(self, event: MessageEvent) -> bool:
        if (
            self._media_batch_delay_seconds <= 0
            or event.message_type != MessageType.TEXT
            or event.is_command()
        ):
            return False
        return self._pending_media_batch_key_for_text(event) is not None

    def _media_batch_session_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=(self.config.extra or {}).get("group_sessions_per_user", True),
            thread_sessions_per_user=(self.config.extra or {}).get("thread_sessions_per_user", False),
        )

    def _media_batch_key(self, event: MessageEvent) -> str:
        session_key = self._media_batch_session_key(event)
        message_type = getattr(event.message_type, "value", str(event.message_type))
        return f"{session_key}:media:{message_type}"

    def _pending_media_batch_key_for_text(self, event: MessageEvent) -> Optional[str]:
        session_key = self._media_batch_session_key(event)
        prefix = f"{session_key}:media:"
        matches = [key for key in self._pending_media_batches if key.startswith(prefix)]
        if not matches:
            return None
        return matches[-1]

    def _media_batch_log_key(self, key: str) -> str:
        session_key, _, _ = key.partition(":media:")
        return f"{session_key}:media"

    async def _merge_text_into_media_batch(self, event: MessageEvent) -> None:
        key = self._pending_media_batch_key_for_text(event)
        if key is None:
            await self.handle_message(event)
            return
        existing = self._pending_media_batches.get(key)
        if existing is None:
            await self.handle_message(event)
            return
        if event.text:
            existing.text = self._merge_caption(existing.text, event.text)
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._schedule_media_batch_flush(key)

    async def _enqueue_media_event(self, event: MessageEvent) -> None:
        key = self._media_batch_key(event)
        existing = self._pending_media_batches.get(key)
        if existing is None:
            self._pending_media_batches[key] = event
            self._schedule_media_batch_flush(key)
            return

        existing.media_urls.extend(event.media_urls)
        existing.media_types.extend(event.media_types)
        if event.text:
            existing.text = self._merge_caption(existing.text, event.text)
        existing.timestamp = event.timestamp
        if event.message_id:
            existing.message_id = event.message_id
        self._schedule_media_batch_flush(key)

    def _schedule_media_batch_flush(self, key: str) -> None:
        prior_task = self._pending_media_batch_tasks.get(key)
        if prior_task is not None and not prior_task.done():
            prior_task.cancel()
        self._pending_media_batch_tasks[key] = asyncio.create_task(
            self._flush_media_batch(key)
        )

    async def _flush_media_batch(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._media_batch_delay_seconds)
            await self._flush_media_batch_now(key)
        finally:
            if self._pending_media_batch_tasks.get(key) is current_task:
                self._pending_media_batch_tasks.pop(key, None)

    async def _flush_media_batch_now(self, key: str) -> None:
        event = self._pending_media_batches.pop(key, None)
        if event is None:
            return
        logger.info(
            "[photon] flushing inbound media batch %s with %d attachment(s)",
            self._media_batch_log_key(key),
            len(event.media_urls),
        )
        await self.handle_message(event)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        cutoff = now - _DEDUP_WINDOW_SECONDS
        stale = [
            seen_id
            for seen_id, seen_at in self._seen_messages.items()
            if seen_at < cutoff
        ]
        for seen_id in stale:
            self._seen_messages.pop(seen_id, None)
        if message_id in self._seen_messages:
            self._seen_messages[message_id] = now
            return True
        self._seen_messages[message_id] = now
        if len(self._seen_messages) > _DEDUP_MAX_SIZE:
            oldest = sorted(self._seen_messages.items(), key=lambda item: item[1])
            for seen_id, _seen_at in oldest[: len(self._seen_messages) - _DEDUP_MAX_SIZE]:
                self._seen_messages.pop(seen_id, None)
        return False

    def _message_event_from_sdk_event(
        self,
        payload: Dict[str, Any],
        *,
        message_id: str,
    ) -> MessageEvent:
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        space = _first_dict(payload.get("space"), message.get("space"))
        sender = _first_dict(payload.get("sender"), message.get("sender"), message.get("from"))
        content = _first_dict(payload.get("content"), message.get("content"))

        space_id = _first_nonempty_string(
            space.get("id"),
            payload.get("spaceId"),
            payload.get("space_id"),
            message.get("spaceId"),
            message.get("chatId"),
            message.get("conversationId"),
        )
        if not space_id:
            raise ValueError("missing Spectrum space id")

        sender_id = _first_nonempty_string(
            sender.get("id"),
            sender.get("userId"),
            sender.get("phone"),
            sender.get("address"),
            payload.get("senderId"),
            message.get("senderId"),
            message.get("fromId"),
        )
        sender_name = _first_nonempty_string(
            sender.get("name"),
            sender.get("displayName"),
            sender.get("handle"),
            sender_id,
        )
        chat_name = _first_nonempty_string(space.get("name"), space.get("displayName"), space_id)
        chat_type = _chat_type_for_space(space_id, space)
        timestamp = _parse_timestamp(
            _first_nonempty_string(
                payload.get("timestamp"),
                payload.get("createdAt"),
                message.get("timestamp"),
                message.get("createdAt"),
                message.get("created_at"),
            )
        )
        text, message_type = _content_to_text_and_type(payload, message, content)
        media_urls = _content_media_urls(content)
        media_types = _content_media_types(content, media_urls)

        source = self.build_source(
            chat_id=space_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id or space_id,
            user_name=sender_name or None,
            message_id=message_id,
        )
        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=message_id,
            raw_message=payload,
            timestamp=timestamp,
            media_urls=media_urls,
            media_types=media_types,
        )

    # -- Outbound ----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,  # noqa: ARG002
    ) -> SendResult:
        if not chat_id:
            return SendResult(success=False, error="Photon chat_id is required")
        if not isinstance(content, str):
            return SendResult(success=False, error="Photon content must be text")

        if "MEDIA:" in content:
            media_result = await self._send_media_directive_response(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
            )
            if media_result is not None:
                return media_result

        return await self._send_text_payload(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
        )

    async def _send_text_payload(
        self,
        *,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        text = content
        if len(text) > self.MAX_MESSAGE_LENGTH:
            logger.warning(
                "[photon] truncating outbound from %d to %d chars",
                len(text),
                self.MAX_MESSAGE_LENGTH,
            )
            text = text[: self.MAX_MESSAGE_LENGTH]
        payload: Dict[str, Any] = {"spaceId": chat_id, "text": text}
        if reply_to:
            payload["replyTo"] = reply_to
        try:
            data = await self._sdk_request("send", payload)
        except PhotonAdapterError as e:
            return SendResult(success=False, error=str(e), retryable=e.retryable)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        self._last_send_at = _utc_now_iso()
        self._write_adapter_runtime_state()
        return SendResult(
            success=True,
            message_id=data.get("messageId"),
            raw_response=data.get("raw") or data,
        )

    async def _send_media_directive_response(
        self,
        *,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> Optional[SendResult]:
        force_document_attachments = "[[as_document]]" in content
        media_files, text = self.extract_media(content)
        photon_media_files, text = _extract_photon_media_directives(text)
        media_files.extend(photon_media_files)
        media_files = self.filter_media_delivery_paths(media_files)

        text = text.replace("[[audio_as_voice]]", "").strip()
        text = text.replace("[[as_document]]", "").strip()

        # If extract_media did not understand the directive, fall back to the
        # normal text sender so unrelated text is not swallowed.
        if "MEDIA:" in text and not media_files:
            return None

        last_result: Optional[SendResult] = None
        if text:
            last_result = await self._send_text_payload(
                chat_id=chat_id,
                content=text,
                reply_to=reply_to,
            )
            if not last_result.success:
                return last_result

        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        for media_path, is_voice in media_files:
            ext = Path(media_path).suffix.lower()
            if is_voice:
                result = await self.send_voice(
                    chat_id=chat_id,
                    audio_path=media_path,
                    reply_to=reply_to,
                )
            elif ext in image_exts and not force_document_attachments:
                result = await self.send_image_file(
                    chat_id=chat_id,
                    image_path=media_path,
                    reply_to=reply_to,
                )
            elif ext in video_exts and not force_document_attachments:
                result = await self.send_video(
                    chat_id=chat_id,
                    video_path=media_path,
                    reply_to=reply_to,
                )
            else:
                result = await self.send_document(
                    chat_id=chat_id,
                    file_path=media_path,
                    reply_to=reply_to,
                )
            if not result.success:
                return result
            last_result = result

        if last_result is not None:
            return last_result
        return SendResult(success=True)

    async def _sidecar_send_attachment(
        self,
        *,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        as_voice: bool = False,
    ) -> SendResult:
        try:
            payload = _attachment_command_payload(
                chat_id=chat_id,
                file_path=file_path,
                caption=caption,
                file_name=file_name,
                reply_to=reply_to,
                as_voice=as_voice,
            )
        except ValueError as e:
            return SendResult(success=False, error=str(e))

        try:
            data = await self._sdk_request(
                "send_attachment",
                payload,
                timeout=_SIDECAR_ATTACHMENT_TIMEOUT_SECONDS,
            )
        except PhotonAdapterError as e:
            return SendResult(success=False, error=str(e), retryable=e.retryable)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        self._last_send_at = _utc_now_iso()
        self._write_adapter_runtime_state()
        return SendResult(
            success=True,
            message_id=data.get("messageId") or data.get("captionMessageId"),
            raw_response=data.get("raw") or data,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,  # noqa: ARG002
    ) -> SendResult:
        try:
            from gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)
        return await self._sidecar_send_attachment(
            chat_id=chat_id,
            file_path=local_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._sidecar_send_attachment(
            chat_id=chat_id,
            file_path=image_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._sidecar_send_attachment(
            chat_id=chat_id,
            file_path=audio_path,
            caption=caption,
            reply_to=reply_to,
            as_voice=True,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._sidecar_send_attachment(
            chat_id=chat_id,
            file_path=video_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._sidecar_send_attachment(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:  # noqa: ANN001, ARG002
        if not chat_id:
            return
        try:
            await self._sdk_request("typing", {"spaceId": chat_id}, timeout=5.0)
        except Exception as e:
            logger.debug("[photon] send_typing failed: %s", e)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if ";+;" in chat_id else "dm"
        return {"name": chat_id, "type": chat_type, "id": chat_id}

    # -- Runtime status ----------------------------------------------------

    def adapter_status(self) -> Dict[str, Any]:
        proc = self._sidecar_proc
        running = bool(proc is not None and proc.returncode is None)
        return {
            "schema_version": 1,
            "state": self._adapter_state,
            "healthy": running and self._sdk_connected and self._adapter_state == "connected",
            "project_name": self._project_name,
            "project_id": self._project_id,
            "operator_phone": self._operator_phone,
            "pid": proc.pid if proc is not None else None,
            "started_at": self._started_at,
            "updated_at": _utc_now_iso(),
            "sdk": {
                "connected": self._sdk_connected,
            },
            "last_event_at": self._last_event_at,
            "last_send_at": self._last_send_at,
            "last_error": self._last_error,
        }

    def _write_adapter_runtime_state(self) -> None:
        path = adapter_runtime_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            state = {
                "pid": self._sidecar_proc.pid if self._sidecar_proc is not None else None,
                "project_id": self._project_id,
                "operator_phone": self._operator_phone,
                "start_time": self._started_at,
                "health": self.adapter_status(),
            }
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
            tmp.replace(path)
        except Exception as e:
            logger.debug("[photon] failed to write adapter runtime state: %s", e)


# ---------------------------------------------------------------------------
# Normalization helpers


def _adapter_error_from_payload(payload: Dict[str, Any]) -> PhotonAdapterError:
    code = str(payload.get("code") or "SDK_ERROR")
    message = str(payload.get("message") or "Photon Spectrum SDK error")
    retryable = bool(payload.get("retryable", True))
    if retryable:
        return RetryableAdapterError(code, message)
    return PermanentAdapterError(code, message)


def _first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_nonempty_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_message_id(payload: Dict[str, Any]) -> str:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    return _first_nonempty_string(
        payload.get("id"),
        payload.get("messageId"),
        payload.get("message_id"),
        message.get("id"),
        message.get("messageId"),
        message.get("uuid"),
    )


def _stable_event_id(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return "synthetic:" + hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _chat_type_for_space(space_id: str, space: Dict[str, Any]) -> str:
    raw_type = str(space.get("type") or "").lower()
    if raw_type in {"group", "room", "channel"}:
        return "group"
    if ";+;" in space_id:
        return "group"
    return "dm"


def _content_to_text_and_type(
    payload: Dict[str, Any],
    message: Dict[str, Any],
    content: Dict[str, Any],
) -> tuple[str, MessageType]:
    direct_text = _first_nonempty_string(
        payload.get("text"),
        message.get("text"),
        message.get("body"),
        message.get("message"),
    )
    if direct_text:
        return direct_text, MessageType.TEXT

    content_type = str(content.get("type") or content.get("kind") or "").lower()
    if content_type == "text":
        return (
            _first_nonempty_string(content.get("text"), content.get("body"), content.get("value")),
            MessageType.TEXT,
        )
    if content_type == "group":
        return _group_content_to_text_and_type(content)
    if content_type == "voice":
        name = _first_nonempty_string(content.get("name"), content.get("filename"), "(unnamed)")
        mime = _first_nonempty_string(
            content.get("mimeType"),
            content.get("mime"),
            content.get("contentType"),
        )
        return (
            _attachment_marker("Photon voice attachment received", name, mime, content),
            MessageType.VOICE,
        )
    if content_type in {"attachment", "file", "image", "video", "audio"}:
        name = _first_nonempty_string(content.get("name"), content.get("filename"), "(unnamed)")
        mime = _first_nonempty_string(
            content.get("mimeType"),
            content.get("mime"),
            content.get("contentType"),
        )
        return (
            _attachment_marker("Photon attachment received", name, mime, content),
            _attachment_message_type(mime),
        )
    if content_type:
        text = _first_nonempty_string(content.get("text"), content.get("body"), content.get("name"))
        return (
            text or f"[Photon content type not handled: {content_type}]",
            MessageType.TEXT,
        )
    return "[Photon message contained no text payload]", MessageType.TEXT


def _group_content_to_text_and_type(content: Dict[str, Any]) -> tuple[str, MessageType]:
    items = content.get("items")
    if not isinstance(items, list):
        items = []
    text_parts: list[str] = []
    message_type = MessageType.TEXT
    for item in items:
        if not isinstance(item, dict):
            continue
        item_content = _first_dict(item.get("content"), item)
        if not item_content:
            continue
        item_text, item_type = _content_to_text_and_type({}, {}, item_content)
        if item_text and item_text != "[Photon message contained no text payload]":
            text_parts.append(item_text)
        message_type = _merge_group_message_type(message_type, item_type)

    text = "\n".join(text_parts).strip()
    if text:
        return text, message_type
    if items:
        return f"[Photon grouped message received: {len(items)} item(s)]", message_type
    return "[Photon grouped message received with no supported items]", MessageType.TEXT


def _merge_group_message_type(current: MessageType, incoming: MessageType) -> MessageType:
    priority = {
        MessageType.TEXT: 0,
        MessageType.DOCUMENT: 1,
        MessageType.AUDIO: 2,
        MessageType.VOICE: 2,
        MessageType.VIDEO: 3,
        MessageType.PHOTO: 4,
    }
    return incoming if priority.get(incoming, 0) > priority.get(current, 0) else current


def _attachment_marker(prefix: str, name: str, mime: str, content: Dict[str, Any]) -> str:
    parts = [f"[{prefix}: {name} ({mime or 'unknown type'})]"]
    local_path = _first_nonempty_string(content.get("localPath"), content.get("path"))
    if local_path:
        parts.append(f"[Cached at: {local_path}]")
    cache_error = _first_nonempty_string(content.get("cacheError"))
    if cache_error:
        parts.append(f"[Attachment cache failed: {cache_error}]")
    return "\n".join(parts)


def _content_media_urls(content: Dict[str, Any]) -> list[str]:
    content_type = str(content.get("type") or content.get("kind") or "").lower()
    if content_type == "group":
        media_urls: list[str] = []
        for item in content.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_content = _first_dict(item.get("content"), item)
            if item_content:
                media_urls.extend(_content_media_urls(item_content))
        return media_urls
    local_path = _first_nonempty_string(content.get("localPath"), content.get("path"))
    if local_path and Path(local_path).is_file():
        return [local_path]
    return []


def _content_media_types(content: Dict[str, Any], media_urls: list[str]) -> list[str]:
    content_type = str(content.get("type") or content.get("kind") or "").lower()
    if content_type == "group":
        media_types: list[str] = []
        for item in content.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_content = _first_dict(item.get("content"), item)
            if not item_content:
                continue
            item_urls = _content_media_urls(item_content)
            media_types.extend(_content_media_types(item_content, item_urls))
        return media_types
    if not media_urls:
        return []
    mime = _first_nonempty_string(
        content.get("mimeType"),
        content.get("mime"),
        content.get("contentType"),
    )
    return [mime] if mime else [""]


def _attachment_message_type(mime: str) -> MessageType:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return MessageType.PHOTO
    if mime.startswith("video/"):
        return MessageType.VIDEO
    if mime.startswith("audio/"):
        return MessageType.AUDIO
    if mime.startswith("application/"):
        return MessageType.DOCUMENT
    return MessageType.DOCUMENT


def _guess_mime_type(file_path: str, file_name: Optional[str] = None) -> str:
    candidates = [file_name, os.path.basename(file_path), file_path]
    for candidate in candidates:
        if not candidate:
            continue
        guessed, _ = mimetypes.guess_type(str(candidate))
        if guessed:
            return guessed
    return "application/octet-stream"


def _attachment_command_payload(
    *,
    chat_id: str,
    file_path: str,
    caption: Optional[str] = None,
    file_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    as_voice: bool = False,
) -> Dict[str, Any]:
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise ValueError("Photon chat_id is required")
    path = Path(str(file_path)).expanduser()
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")
    name = (file_name or path.name or "attachment").strip()
    payload: Dict[str, Any] = {
        "spaceId": chat_id.strip(),
        "filePath": str(path),
        "fileName": name,
        "mimeType": _guess_mime_type(str(path), name),
        "asVoice": bool(as_voice),
    }
    if caption:
        payload["caption"] = str(caption)
    if reply_to:
        payload["replyTo"] = str(reply_to)
    return payload


def _normalize_standalone_media(media_files: Optional[list]) -> list[tuple[str, bool]]:
    normalized: list[tuple[str, bool]] = []
    for item in media_files or []:
        if isinstance(item, (list, tuple)) and item:
            media_path = item[0]
            is_voice = bool(item[1]) if len(item) > 1 else False
        else:
            media_path = item
            is_voice = False
        if media_path is None:
            continue
        path = str(media_path).strip()
        if path:
            normalized.append((path, is_voice))
    return normalized


# Standalone out-of-process delivery


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send one Photon message without opening an inbound Spectrum stream."""
    _ = thread_id
    _ = force_document
    normalized_media = _normalize_standalone_media(media_files)
    if not isinstance(chat_id, str) or not chat_id.strip():
        return {"error": "Photon standalone send: chat_id is required"}
    if not isinstance(message, str):
        return {"error": "Photon standalone send: text content must be a string"}
    if not message.strip() and not normalized_media:
        return {"error": "Photon standalone send: text content is required"}

    project_id, project_secret = _configured_project_credentials(pconfig)
    if not (project_id and project_secret):
        return {
            "error": (
                "Photon standalone send: PHOTON_PROJECT_ID and "
                "PHOTON_PROJECT_SECRET are required"
            )
        }

    node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node") or "node"
    if not shutil.which(node_bin):
        return {"error": "Photon standalone send: Node.js is required"}
    if not (_SIDECAR_DIR / "node_modules").exists():
        return {
            "error": (
                "Photon standalone send: sidecar deps are not installed. "
                f"Run: cd {_SIDECAR_DIR} && npm install"
            )
        }

    attachment_payloads: list[Dict[str, Any]] = []
    for media_path, is_voice in normalized_media:
        try:
            attachment_payloads.append(
                _attachment_command_payload(
                    chat_id=chat_id,
                    file_path=media_path,
                    as_voice=is_voice,
                )
            )
        except ValueError as e:
            return {"error": f"Photon standalone send: {e}"}

    text = message
    if len(text) > _MAX_MESSAGE_LENGTH:
        logger.warning(
            "[photon] truncating standalone outbound from %d to %d chars",
            len(text),
            _MAX_MESSAGE_LENGTH,
        )
        text = text[:_MAX_MESSAGE_LENGTH]

    try:
        data = await _send_once_via_sidecar(
            node_bin=node_bin,
            project_id=project_id,
            project_secret=project_secret,
            chat_id=chat_id.strip(),
            text=text,
            attachments=attachment_payloads,
        )
    except PhotonAdapterError as e:
        return {"error": f"Photon standalone send failed: {e}"}
    except Exception as e:
        return {"error": f"Photon standalone send failed: {e}"}

    return {
        "success": True,
        "message_id": data.get("messageId") or data.get("message_id"),
        "raw_response": data.get("raw") or data,
    }


async def _send_once_via_sidecar(
    *,
    node_bin: str,
    project_id: str,
    project_secret: str,
    chat_id: str,
    text: str,
    attachments: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    env = _adapter_process_env(project_id=project_id, project_secret=project_secret)
    request_id = uuid.uuid4().hex
    proc = await asyncio.create_subprocess_exec(
        node_bin,
        str(_SIDECAR_ENTRYPOINT),
        "--send-once",
        cwd=str(_SIDECAR_DIR),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=(sys.platform != "win32"),
    )
    stderr_task = asyncio.create_task(_drain_send_once_stderr(proc))
    try:
        await _wait_for_send_once_ready(proc)
        if proc.stdin is None:
            raise AdapterUnavailableError("Photon send-once sidecar stdin unavailable")
        responses: list[Dict[str, Any]] = []
        if text.strip():
            body = {"requestId": request_id, "type": "send", "spaceId": chat_id, "text": text}
            proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            responses.append(await _wait_for_send_once_response(proc, request_id))
        for attachment_payload in attachments or []:
            attachment_request_id = uuid.uuid4().hex
            body = {
                "requestId": attachment_request_id,
                "type": "send_attachment",
                **attachment_payload,
            }
            proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            responses.append(
                await _wait_for_send_once_response(proc, attachment_request_id)
            )
        if not responses:
            raise PermanentAdapterError("BAD_PAYLOAD", "nothing to send")
        last = responses[-1]
        return {
            **last,
            "results": responses,
        }
    finally:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SIDECAR_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _terminate_send_once_sidecar(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)


async def _wait_for_send_once_ready(proc: asyncio.subprocess.Process) -> None:
    while True:
        payload = await _read_send_once_payload(
            proc,
            timeout=_SIDECAR_READY_TIMEOUT_SECONDS,
        )
        message_type = payload.get("type")
        if message_type == "ready":
            return
        if message_type == "fatal":
            raise _adapter_error_from_payload(payload.get("error") or {})
        if message_type == "error":
            raise _adapter_error_from_payload(payload.get("error") or {})


async def _wait_for_send_once_response(
    proc: asyncio.subprocess.Process,
    request_id: str,
) -> Dict[str, Any]:
    while True:
        payload = await _read_send_once_payload(
            proc,
            timeout=_SIDECAR_REQUEST_TIMEOUT_SECONDS,
        )
        message_type = payload.get("type")
        if message_type == "response" and str(payload.get("requestId") or "") == request_id:
            if payload.get("ok"):
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise BadAdapterResponseError("sidecar response data was not an object")
                return data
            raise _adapter_error_from_payload(payload.get("error") or {})
        if message_type == "fatal":
            raise _adapter_error_from_payload(payload.get("error") or {})


async def _read_send_once_payload(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> Dict[str, Any]:
    if proc.stdout is None:
        raise AdapterUnavailableError("Photon send-once sidecar stdout unavailable")
    try:
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise RetryableAdapterError(
            "SDK_REQUEST_TIMEOUT",
            "Photon send-once sidecar timed out",
        ) from e
    if not raw:
        code = proc.returncode
        if code is None:
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                code = None
        raise RetryableAdapterError(
            "SDK_SIDECAR_EXITED",
            f"Photon send-once sidecar exited before responding"
            + (f" with code {code}" if code is not None else ""),
        )
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return await _read_send_once_payload(proc, timeout=timeout)
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as e:
        raise BadAdapterResponseError(f"sidecar emitted non-json output: {line}") from e
    if not isinstance(payload, dict):
        raise BadAdapterResponseError("sidecar emitted non-object JSON")
    return payload


async def _drain_send_once_stderr(proc: asyncio.subprocess.Process) -> None:
    if proc.stderr is None:
        return
    try:
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            logger.info("[photon-send-once] %s", raw.decode("utf-8", "replace").rstrip())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("[photon-send-once] stderr drain failed", exc_info=True)


def _terminate_send_once_sidecar(proc: asyncio.subprocess.Process) -> None:
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.terminate()


# Plugin entry point


def register(ctx) -> None:
    """Called by the Hermes plugin loader at startup."""
    from . import cli as _cli

    ctx.register_platform(
        name="photon",
        label="iMessage (via Photon)",
        adapter_factory=lambda cfg: PhotonAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"],
        install_hint=(
            "Run `hermes photon login`, then `hermes photon setup "
            "'+<country-code><number>'` to create/adopt the fixed "
            "hermes-agent Spectrum project, link your phone number, and "
            "install the local sidecar."
        ),
        setup_fn=_cli.interactive_setup,
        env_enablement_fn=_env_enablement,
        allowed_users_env="PHOTON_ALLOWED_USERS",
        allow_all_env="PHOTON_ALLOW_ALL_USERS",
        cron_deliver_env_var="PHOTON_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=_MAX_MESSAGE_LENGTH,
        emoji="📱",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via Photon Spectrum (iMessage). "
            "Treat replies like regular text messages - short, friendly, no "
            "markdown rendering. Recipient identifiers are E.164 phone "
            "numbers; never expose them in responses unless the user asked. "
            "Inbound attachments surface as cached local media when available. "
            "You CAN send media files natively in this iMessage conversation: "
            "to deliver a generated image, PDF, audio file, video, or document "
            "to the user, include MEDIA:/absolute/path/to/file in your final "
            "response. Images (.jpg, .jpeg, .png, .webp, .gif, .heic) arrive "
            "as iMessage attachments; audio/video and other files arrive as "
            "attachments. Do NOT tell the user Photon lacks file-sending "
            "capability, and do NOT use the send_message tool for media in "
            "the current iMessage conversation."
        ),
    )

    ctx.register_cli_command(
        name="photon",
        help="Set up and manage the Photon iMessage integration",
        setup_fn=_cli.register_cli,
        handler_fn=_cli.dispatch,
    )
