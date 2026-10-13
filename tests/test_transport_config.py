from datetime import datetime, timezone

import pytest

from spatius import (
    AgoraTransportConfig,
    LiveKitTransportConfig,
    SessionConfig,
    WebSocketTransportConfig,
    new_avatar_session,
)


def test_websocket_is_the_default_and_region_endpoints_use_v3():
    default = SessionConfig(region="us-west")
    explicit = SessionConfig(region="cn-east", transport=WebSocketTransportConfig())
    assert default.transport is None
    assert (
        default.ingress_endpoint_url == "wss://api.us-west.spatius.ai/v3/driveningress"
    )
    assert (
        explicit.ingress_endpoint_url
        == "wss://api.cn-east.spatialwalk.top/v3/driveningress"
    )
    assert isinstance(explicit.transport, WebSocketTransportConfig)


def test_explicit_ingress_url_is_preserved():
    config = SessionConfig(
        region="us-west", ingress_endpoint_url="ws://localhost/session"
    )
    assert config.ingress_endpoint_url == "ws://localhost/session"


def test_factory_passes_transport_and_keeps_encoder_bitrate_local():
    transport = AgoraTransportConfig(
        channel_name="room", token="test-token", user_account="123"
    )
    session = new_avatar_session(
        transport=transport,
        sample_rate=24000,
        bitrate=32000,
        expire_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert session.config.transport is transport
    assert session.config.sample_rate == 24000
    assert session.config.bitrate == 32000


def test_unsupported_transport_is_rejected():
    with pytest.raises(ValueError, match="transport"):
        SessionConfig(transport="livekit")


@pytest.mark.parametrize("scheme", ["ws", "wss", "http", "https"])
def test_livekit_accepts_server_url_schemes_and_copies_attributes(scheme):
    attributes = {"role": "avatar", "old": ""}
    config = LiveKitTransportConfig(
        url=f"{scheme}://rtc.example.com",
        token="secret-token",
        idle_timeout=60,
        agent_identity="agent",
        extra_attributes=attributes,
    )
    attributes["role"] = "changed"
    assert config.extra_attributes == {"role": "avatar", "old": ""}
    assert config.agent_identity == "agent"
    assert "secret-token" not in repr(config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": ""},
        {"url": "ftp://rtc.example.com"},
        {"url": "rtc.example.com"},
        {"url": "wss://"},
        {"url": "wss://rtc.example.com:bad"},
        {"url": "wss://rtc.example.com/ whitespace"},
        {"token": ""},
        {"token": None},
        {"idle_timeout": -1},
        {"idle_timeout": 2**31},
        {"idle_timeout": True},
        {"idle_timeout": 1.5},
        {"agent_identity": None},
        {"extra_attributes": {"key": 1}},
        {"extra_attributes": {1: "value"}},
        {"extra_attributes": None},
    ],
)
def test_livekit_rejects_invalid_configuration(overrides):
    args = {"url": "wss://rtc.example.com", "token": "test-token"}
    args.update(overrides)
    with pytest.raises(ValueError):
        LiveKitTransportConfig(**args)


@pytest.mark.parametrize("uid", [1, 123, 2**32 - 1])
def test_agora_accepts_explicit_numeric_uid(uid):
    config = AgoraTransportConfig(channel_name="room", token="secret-token", uid=uid)
    assert config.uid == uid
    assert config.user_account is None
    assert "secret-token" not in repr(config)


@pytest.mark.parametrize(
    "account", ["123", "0", "CaseSensitive", "a" * 255, " !#$%&()+,-.:;<=>?@[]^_{|}~"]
)
def test_agora_preserves_string_accounts(account):
    config = AgoraTransportConfig(
        channel_name="room", token="test-token", user_account=account
    )
    assert config.user_account == account
    assert config.uid is None


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"uid": 123, "user_account": "123"},
        {"uid": 0},
        {"uid": -1},
        {"uid": 2**32},
        {"uid": True},
        {"uid": 1.5},
        {"uid": "123"},
        {"user_account": ""},
        {"user_account": "a" * 256},
        {"user_account": "用户"},
        {"user_account": "user/account"},
        {"user_account": "user\naccount"},
        {"user_account": 123},
    ],
)
def test_agora_rejects_invalid_identity(identity):
    with pytest.raises(ValueError):
        AgoraTransportConfig(channel_name="room", token="test-token", **identity)


@pytest.mark.parametrize("overrides", [{"channel_name": ""}, {"token": ""}])
def test_agora_requires_channel_and_token(overrides):
    args = {"channel_name": "room", "token": "test-token", "uid": 123}
    args.update(overrides)
    with pytest.raises(ValueError):
        AgoraTransportConfig(**args)
