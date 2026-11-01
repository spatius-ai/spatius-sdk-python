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


def test_audio_input_and_interrupt_match_v3_wire_format():
    audio = message_pb2.ClientAudioInput(req_id="r", end=True, audio=b"a")
    assert audio.SerializeToString() == bytes.fromhex("0a017210011a0161")
    assert _fields(message_pb2.ClientAudioInput) == {"req_id": 1, "end": 2, "audio": 3}
    interrupt = message_pb2.Message(
        type=message_pb2.MESSAGE_CLIENT_INTERRUPT,
        client_interrupt=message_pb2.ClientInterrupt(req_id="r"),
    )
    assert interrupt.SerializeToString() == bytes.fromhex("08063a030a0172")


def test_conversation_events_use_v3_types_and_field_numbers():
    assert {
        name: message_pb2.MessageType.Value(name)
        for name in (
            "MESSAGE_CLIENT_USER_SPEAK_START",
            "MESSAGE_CLIENT_TRIGGER_REACTION",
            "MESSAGE_CLIENT_USER_SPEAK_END",
            "MESSAGE_CLIENT_SET_EXPRESSION",
        )
    } == {
        "MESSAGE_CLIENT_USER_SPEAK_START": 7,
        "MESSAGE_CLIENT_TRIGGER_REACTION": 8,
        "MESSAGE_CLIENT_USER_SPEAK_END": 9,
        "MESSAGE_CLIENT_SET_EXPRESSION": 10,
    }
    envelope = _fields(message_pb2.Message)
    assert {
        name: envelope[name]
        for name in (
            "client_user_speak_start",
            "client_trigger_reaction",
            "client_user_speak_end",
            "client_set_expression",
        )
    } == {
        "client_user_speak_start": 8,
        "client_trigger_reaction": 9,
        "client_user_speak_end": 10,
        "client_set_expression": 11,
    }
    assert _fields(message_pb2.Expression) == {"label": 1, "strength": 2}
    assert _fields(message_pb2.ClientSetExpression) == {"req_id": 1, "expressions": 2}
    assert _fields(message_pb2.ClientTriggerReaction) == {"req_id": 1, "reactions": 2}
    assert _fields(message_pb2.ClientUserSpeakStart) == {"req_id": 1}
    assert _fields(message_pb2.ClientUserSpeakEnd) == {"req_id": 1}
    # strength is a plain float: an omitted value means zero on the wire
    expression = message_pb2.ClientSetExpression(
        req_id="r", expressions=[message_pb2.Expression(label="happy", strength=0.0)]
    )
    decoded = message_pb2.ClientSetExpression.FromString(expression.SerializeToString())
    assert [(e.label, e.strength) for e in decoded.expressions] == [("happy", 0.0)]


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
    assert _fields(message_pb2.MotionFrame) == {
        **dict(zip(frame_fields, range(1, 8))),
        "pbx": 8,
    }
    for name in frame_fields:
        descriptor = message_pb2.MotionFrame.DESCRIPTOR.fields_by_name[name]
        assert descriptor.type == FieldDescriptor.TYPE_FLOAT
        assert descriptor.GetOptions().packed
    pbx = message_pb2.MotionFrame.DESCRIPTOR.fields_by_name["pbx"]
    assert pbx.type == FieldDescriptor.TYPE_BYTES
    frame = message_pb2.MotionFrame(
        **{name: [0.25, 0.5] for name in frame_fields}, pbx=bytes(range(256)) * 2
    )
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
