"""Tests for iMessage platform adapter."""
import base64
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestIMessagePlatformEnum:
    def test_imessage_enum_exists(self):
        assert Platform.IMESSAGE.value == "imessage"

    def test_imessage_in_platform_list(self):
        platforms = [p.value for p in Platform]
        assert "imessage" in platforms


class TestIMessageConfigLoading:
    def test_apply_env_overrides_local_mode(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.IMESSAGE in config.platforms
        pc = config.platforms[Platform.IMESSAGE]
        assert pc.enabled is True
        assert pc.extra.get("local", True) is True

    def test_apply_env_overrides_remote_mode(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_SERVER_URL", "https://imessage.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", "test-key")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.IMESSAGE in config.platforms
        pc = config.platforms[Platform.IMESSAGE]
        assert pc.enabled is True
        assert pc.extra.get("local") is False
        assert pc.extra.get("server_url") == "https://imessage.example.com"
        assert pc.api_key == "test-key"

    def test_not_loaded_without_env(self, monkeypatch):
        monkeypatch.delenv("IMESSAGE_ENABLED", raising=False)
        monkeypatch.delenv("IMESSAGE_SERVER_URL", raising=False)
        monkeypatch.delenv("IMESSAGE_API_KEY", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.IMESSAGE not in config.platforms

    def test_connected_platforms_includes_imessage(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        import gateway.config as gw_config
        monkeypatch.setattr(gw_config, "platform_mod", type("_FakePlatform", (), {"system": staticmethod(lambda: "Darwin")})())

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        connected = config.get_connected_platforms()
        assert Platform.IMESSAGE in connected

    def test_home_channel_from_env(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_HOME_CHANNEL", "+15551234567")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        pc = config.platforms[Platform.IMESSAGE]
        assert pc.home_channel is not None
        assert pc.home_channel.chat_id == "+15551234567"


# ---------------------------------------------------------------------------
# Adapter Init & Helpers
# ---------------------------------------------------------------------------

class TestIMessageAdapterInit:
    def _make_config(self, local=True, **extra):
        config = PlatformConfig()
        config.enabled = True
        config.extra = {"local": local, **extra}
        return config

    def test_init_local_mode(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config(local=True))
        assert adapter._local is True
        assert adapter._server_url == ""

    def test_init_remote_mode(self):
        from gateway.platforms.imessage import IMessageAdapter
        config = self._make_config(
            local=False,
            server_url="https://imessage.example.com",
        )
        config.api_key = "test-key"
        adapter = IMessageAdapter(config)
        assert adapter._local is False
        assert adapter._server_url == "https://imessage.example.com"
        assert adapter._api_key == "test-key"

    def test_init_default_poll_interval(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config())
        assert adapter._poll_interval == 2.0

    def test_init_custom_poll_interval(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config(poll_interval=5.0))
        assert adapter._poll_interval == 5.0

    def test_init_group_policy_default(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config())
        assert adapter._group_policy == "open"

    def test_init_group_policy_ignore(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config(group_policy="ignore"))
        assert adapter._group_policy == "ignore"

    def test_init_consecutive_errors_zero(self):
        from gateway.platforms.imessage import IMessageAdapter
        adapter = IMessageAdapter(self._make_config())
        assert adapter._consecutive_errors == 0


class TestIMessageHelpers:
    def test_check_requirements(self):
        from gateway.platforms.imessage import check_imessage_requirements
        assert check_imessage_requirements() is True

    def test_make_bearer_token(self):
        from gateway.platforms.imessage import _make_bearer_token
        token = _make_bearer_token("https://example.com", "my-key")
        decoded = base64.b64decode(token).decode()
        assert decoded == "https://example.com|my-key"

    def test_make_bearer_token_passthrough(self):
        from gateway.platforms.imessage import _make_bearer_token
        pre_encoded = base64.b64encode(b"https://s.com|key123").decode()
        token = _make_bearer_token("https://s.com", pre_encoded)
        assert token == pre_encoded

    def test_extract_address_dm(self):
        from gateway.platforms.imessage import _extract_address
        assert _extract_address("iMessage;-;+1234567890") == "+1234567890"

    def test_extract_address_group(self):
        from gateway.platforms.imessage import _extract_address
        assert _extract_address("iMessage;+;chat123") == "group:chat123"

    def test_extract_address_passthrough_phone(self):
        from gateway.platforms.imessage import _extract_address
        assert _extract_address("+1234567890") == "+1234567890"

    def test_extract_address_passthrough_email(self):
        from gateway.platforms.imessage import _extract_address
        assert _extract_address("user@icloud.com") == "user@icloud.com"

    def test_split_paragraphs(self):
        from gateway.platforms.imessage import _split_paragraphs
        result = _split_paragraphs("Hello\n\nWorld\n\nFoo")
        assert result == ["Hello", "World", "Foo"]

    def test_split_paragraphs_single(self):
        from gateway.platforms.imessage import _split_paragraphs
        result = _split_paragraphs("Hello world")
        assert result == ["Hello world"]

    def test_split_paragraphs_empty(self):
        from gateway.platforms.imessage import _split_paragraphs
        result = _split_paragraphs("")
        assert result == [""]

    def test_dedup_tracking(self):
        from gateway.platforms.imessage import IMessageAdapter
        config = PlatformConfig()
        config.enabled = True
        config.extra = {"local": True}
        adapter = IMessageAdapter(config)

        assert adapter._is_seen("msg-1") is False
        adapter._mark_seen("msg-1")
        assert adapter._is_seen("msg-1") is True

    def test_dedup_eviction(self):
        from gateway.platforms.imessage import IMessageAdapter
        config = PlatformConfig()
        config.enabled = True
        config.extra = {"local": True}
        adapter = IMessageAdapter(config)

        for i in range(1100):
            adapter._mark_seen(f"msg-{i}")

        assert adapter._is_seen("msg-0") is False
        assert adapter._is_seen("msg-1099") is True
        assert len(adapter._processed_ids) == 1000

    def test_backoff_delay_increases(self):
        from gateway.platforms.imessage import IMessageAdapter, _BACKOFF_MAX
        config = PlatformConfig()
        config.enabled = True
        config.extra = {"local": True}
        adapter = IMessageAdapter(config)

        adapter._consecutive_errors = 1
        d1 = adapter._backoff_delay()
        adapter._consecutive_errors = 5
        d5 = adapter._backoff_delay()
        assert d5 > d1
        assert d5 <= _BACKOFF_MAX


# ---------------------------------------------------------------------------
# PII Redaction
# ---------------------------------------------------------------------------

class TestIMessageRedaction:
    @pytest.fixture(autouse=True)
    def _ensure_redaction_enabled(self, monkeypatch):
        monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)

    def test_phone_number_redacted(self):
        from agent.redact import redact_sensitive_text
        result = redact_sensitive_text("iMessage from +15551234567")
        assert "+15551234567" not in result
        assert "****" in result

    def test_api_key_in_env_assignment_redacted(self):
        from agent.redact import redact_sensitive_text
        fake_key = "X" * 24
        result = redact_sensitive_text(f"IMESSAGE_API_KEY={fake_key}")
        assert fake_key not in result

    def test_bearer_token_redacted(self):
        from agent.redact import redact_sensitive_text
        import base64
        fake_token = base64.b64encode(b"https://test.example.com|fake-key-000").decode()
        result = redact_sensitive_text(f"Authorization: Bearer {fake_token}")
        assert fake_token not in result


# ---------------------------------------------------------------------------
# PII Safe Platform
# ---------------------------------------------------------------------------

class TestIMessagePIISafe:
    def test_imessage_in_pii_safe(self):
        from gateway.session import _PII_SAFE_PLATFORMS
        assert Platform.IMESSAGE in _PII_SAFE_PLATFORMS


# ---------------------------------------------------------------------------
# Authorization in run.py
# ---------------------------------------------------------------------------

class TestIMessageAuthorization:
    def test_imessage_in_allowlist_maps(self):
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.IMESSAGE
        source.user_id = "+15559999999"

        with patch.dict("os.environ", {}, clear=True):
            result = gw._is_user_authorized(source)
            assert result is False


# ---------------------------------------------------------------------------
# Send Message Tool
# ---------------------------------------------------------------------------

class TestIMessageSendMessage:
    def test_imessage_in_send_message_source(self):
        """Check source text for imessage routing (avoids transitive import issues)."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "tools" / "send_message_tool.py").read_text()
        assert '"imessage"' in src
        assert "Platform.IMESSAGE" in src
        assert "_send_imessage" in src

    def test_imessage_enum_value(self):
        assert Platform.IMESSAGE.value == "imessage"


# ---------------------------------------------------------------------------
# Toolset
# ---------------------------------------------------------------------------

class TestIMessageToolset:
    def test_hermes_imessage_toolset_exists(self):
        from toolsets import TOOLSETS
        assert "hermes-imessage" in TOOLSETS

    def test_hermes_gateway_includes_imessage(self):
        from toolsets import TOOLSETS
        gw = TOOLSETS.get("hermes-gateway", {})
        includes = gw.get("includes", [])
        assert "hermes-imessage" in includes


# ---------------------------------------------------------------------------
# Platform Hint
# ---------------------------------------------------------------------------

class TestIMessagePlatformHint:
    def test_imessage_hint_exists(self):
        from agent.prompt_builder import PLATFORM_HINTS
        assert "imessage" in PLATFORM_HINTS
        assert "iMessage" in PLATFORM_HINTS["imessage"]


# ---------------------------------------------------------------------------
# Cron Delivery
# ---------------------------------------------------------------------------

class TestIMessageCronDelivery:
    def test_imessage_in_cron_platform_map(self):
        from cron.scheduler import _deliver_result
        import inspect
        source = inspect.getsource(_deliver_result)
        assert '"imessage"' in source or "'imessage'" in source

    def test_platform_map_has_imessage(self):
        """Verify the cron scheduler platform_map dict includes imessage."""
        import cron.scheduler as sched
        import inspect
        src = inspect.getsource(sched)
        assert "Platform.IMESSAGE" in src


# ---------------------------------------------------------------------------
# Channel Directory
# ---------------------------------------------------------------------------

class TestIMessageChannelDirectory:
    def test_imessage_in_channel_directory(self):
        import gateway.channel_directory as chdir
        import inspect
        src = inspect.getsource(chdir)
        assert '"imessage"' in src or "'imessage'" in src


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------

class TestIMessageCLIParity:
    def test_imessage_in_tools_config_platforms(self):
        from hermes_cli.tools_config import PLATFORMS
        assert "imessage" in PLATFORMS

    def test_imessage_in_skills_config_platforms(self):
        from hermes_cli.skills_config import PLATFORMS
        assert "imessage" in PLATFORMS

    def test_imessage_in_session_stats_loop(self):
        import hermes_cli.main as main_mod
        import inspect
        src = inspect.getsource(main_mod)
        assert '"imessage"' in src


# ---------------------------------------------------------------------------
# MessageEvent contract
# ---------------------------------------------------------------------------

class TestIMessageEventContract:
    """Verify adapter builds MessageEvent with the correct field names."""

    def test_message_type_photo_exists(self):
        from gateway.platforms.base import MessageType
        assert hasattr(MessageType, "PHOTO")

    def test_message_type_image_not_used(self):
        """Adapter must use PHOTO, not IMAGE (which doesn't exist)."""
        from gateway.platforms.base import MessageType
        assert not hasattr(MessageType, "IMAGE")

    def test_message_event_has_media_urls(self):
        from gateway.platforms.base import MessageEvent
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(MessageEvent)}
        assert "media_urls" in field_names
        assert "media_types" in field_names
        assert "images" not in field_names
        assert "metadata" not in field_names

    def test_message_event_has_reply_to_message_id(self):
        from gateway.platforms.base import MessageEvent
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(MessageEvent)}
        assert "reply_to_message_id" in field_names

    def test_adapter_source_uses_correct_fields(self):
        """Spot-check that adapter code uses media_urls not images."""
        from gateway.platforms import imessage
        import inspect
        src = inspect.getsource(imessage)
        assert "images=image_paths" not in src
        assert "media_urls=" in src
