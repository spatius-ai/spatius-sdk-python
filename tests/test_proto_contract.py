from pathlib import Path

import pytest
from google.protobuf.descriptor import FieldDescriptor

from spatius.proto.generated import message_pb2


def _fields(message_type):
    return {field.name: field.number for field in message_type.DESCRIPTOR.fields}


def test_configuration_uses_compact_v3_field_numbers():
    assert _fields(message_pb2.ClientConfigureSession) == {
        "audio_sample_rate": 1,
        "audio_format": 2,
        "transport": 3,
        "extra_params": 4,
        "trace_context": 5,
    }
    config = message_pb2.ClientConfigureSession(
        audio_sample_rate=16000,
        audio_format=message_pb2.AUDIO_FORMAT_OGG_OPUS,
        transport=message_pb2.TransportConfig(
            websocket=message_pb2.WebSocketTransportConfig()
        ),
        extra_params={"key": "value"},
        trace_context=message_pb2.TraceContext(traceparent="a", tracestate="b"),
    )
    assert config.SerializeToString(deterministic=True) == bytes.fromhex(
        "08807d10011a020a00220c0a036b6579120576616c75652a060a0161120162"
    )


def test_audio_input_emotion_and_interrupt_match_v3_wire_format():
    audio = message_pb2.ClientAudioInput(
        req_id="r", end=True, audio=b"a", emotion="happy"
    )
    assert audio.SerializeToString() == bytes.fromhex("0a017210011a016122056861707079")
    assert _fields(message_pb2.ClientAudioInput) == {
        "req_id": 1,
        "end": 2,
        "audio": 3,
        "emotion": 4,
        "strength": 5,
    }
    interrupt = message_pb2.Message(
        type=message_pb2.MESSAGE_CLIENT_INTERRUPT,
        client_interrupt=message_pb2.ClientInterrupt(req_id="r"),
    )
    assert interrupt.SerializeToString() == bytes.fromhex("08063a030a0172")


@pytest.mark.parametrize(
    ("strength", "wire"),
    [(0.0, "2d00000000"), (0.5, "2d0000003f"), (1.0, "2d0000803f")],
)
def test_audio_strength_preserves_optional_presence_and_float32_wire_value(
    strength, wire
):
    audio = message_pb2.ClientAudioInput()
    assert not audio.HasField("strength")
    assert audio.SerializeToString() == b""
    audio.strength = strength
    assert audio.SerializeToString() == bytes.fromhex(wire)
    decoded = message_pb2.ClientAudioInput.FromString(bytes.fromhex(wire))
    assert decoded.HasField("strength")
    assert decoded.strength == strength
    decoded.ClearField("strength")
    assert not decoded.HasField("strength")
    assert decoded.SerializeToString() == b""


def test_motion_response_preserves_all_frame_values_and_metadata():
    frame_fields = [
        "expr",
        "body_pose",
        "jaw_pose",
        "eyelid",
        "leye_pose",
        "reye_pose",
        "emotion_weights",
    ]
    assert _fields(message_pb2.MotionFrame) == dict(zip(frame_fields, range(1, 8)))
    for descriptor in message_pb2.MotionFrame.DESCRIPTOR.fields:
        assert descriptor.type == FieldDescriptor.TYPE_FLOAT
        assert descriptor.GetOptions().packed
    frame = message_pb2.MotionFrame(**{name: [0.25, 0.5] for name in frame_fields})
    response = message_pb2.ServerResponseMotion(
        connection_id="connection",
        req_id="request",
        end=True,
        animation=message_pb2.MotionAnimation(keyframes=[frame]),
        avatar_id="avatar",
        trace_context=message_pb2.TraceContext(traceparent="trace", tracestate="state"),
        emotion="happy",
    )
    message = message_pb2.Message(
        type=message_pb2.MESSAGE_SERVER_RESPONSE_MOTION,
        server_response_motion=response,
    )
    decoded = message_pb2.Message.FromString(message.SerializeToString())
    assert decoded == message
    assert decoded.WhichOneof("data") == "server_response_motion"
    assert _fields(message_pb2.ServerResponseMotion) == {
        "connection_id": 1,
        "req_id": 2,
        "end": 3,
        "animation": 4,
        "avatar_id": 5,
        "trace_context": 6,
        "emotion": 7,
    }


@pytest.mark.parametrize("uid", [0, 123, 2**32 - 1])
def test_agora_numeric_uid_is_preserved_in_transport_wire_format(uid):
    transport = message_pb2.TransportConfig(
        agora=message_pb2.AgoraTransportConfig(
            channel_name="room", token="test-token", uid=uid
        )
    )
    decoded = message_pb2.TransportConfig.FromString(transport.SerializeToString())
    assert decoded == transport
    assert decoded.WhichOneof("destination") == "agora"
    assert decoded.agora.uid == uid
    assert _fields(message_pb2.AgoraTransportConfig) == {
        "channel_name": 1,
        "token": 2,
        "uid": 3,
    }
    assert not message_pb2.AgoraTransportConfig.DESCRIPTOR.oneofs


def test_livekit_preserves_extra_attributes_and_transport_oneof():
    transport = message_pb2.TransportConfig(
        livekit=message_pb2.LiveKitTransportConfig(
            url="wss://rtc.example.com",
            token="test-token",
            idle_timeout=60,
            agent_identity="agent",
            extra_attributes={"role": "avatar", "remove": ""},
        )
    )
    decoded = message_pb2.TransportConfig.FromString(transport.SerializeToString())
    assert decoded == transport
    decoded.websocket.SetInParent()
    assert decoded.WhichOneof("destination") == "websocket"
    assert not decoded.HasField("livekit")


def test_public_proto_has_no_comments_or_service_dependencies():
    source = Path(__file__).resolve().parents[1] / "proto" / "message.proto"
    text = source.read_text()
    assert "//" not in text
    assert "/*" not in text
    assert not message_pb2.DESCRIPTOR.services_by_name
    assert not message_pb2.DESCRIPTOR.dependencies
