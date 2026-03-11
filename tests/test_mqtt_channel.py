from __future__ import annotations

import json
import ssl
from typing import Any

import pytest
from pydantic import ValidationError

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.mqtt import MQTTChannel
from nanobot.config.schema import MQTTConfig, MQTTTopicConfig


def _build_config(**overrides: Any) -> MQTTConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "host": "localhost",
        "port": 1883,
        "allow_from": ["*"],
    }
    defaults.update(overrides)
    return MQTTConfig(**defaults)


class _FakeAsyncMessages:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self._messages = list(messages or [])

    def __aiter__(self) -> "_FakeAsyncMessages":
        return self

    async def __anext__(self) -> Any:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **connection_options: Any) -> None:
        self.connection_options = connection_options
        self.subscriptions: list[tuple[str, int]] = []
        self.published: list[dict[str, Any]] = []
        self.messages = _FakeAsyncMessages()
        self.disconnect_called = False
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append((topic, qos))

    async def publish(
        self,
        *,
        topic: str,
        payload: str | bytes,
        qos: int,
        retain: bool,
    ) -> None:
        self.published.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain,
            }
        )

    async def disconnect(self) -> None:
        self.disconnect_called = True


def _decode_json_payload(payload: str | bytes) -> dict[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return json.loads(payload)


def test_compile_topic_template_builds_filter_and_regex() -> None:
    mqtt_filter, topic_regex = MQTTChannel._compile_topic_template(
        "nanobot/{device_id}/{session_id}/inbox"
    )

    assert mqtt_filter == "nanobot/+/+/inbox"

    match = topic_regex.fullmatch("nanobot/kitchen/session-1/inbox")
    assert match is not None
    assert match.groupdict() == {
        "device_id": "kitchen",
        "session_id": "session-1",
    }


def test_compile_topic_template_rejects_duplicate_placeholders() -> None:
    with pytest.raises(ValueError, match="duplicated"):
        MQTTChannel._compile_topic_template("nanobot/{chat_id}/{chat_id}/inbox")


def test_compile_topic_template_rejects_mqtt_wildcards() -> None:
    with pytest.raises(ValueError, match="named placeholders"):
        MQTTChannel._compile_topic_template("nanobot/+/inbox")


def test_mqtt_config_rejects_invalid_reconnect_range() -> None:
    with pytest.raises(ValidationError, match="reconnect_max_delay"):
        MQTTConfig(
            enabled=True,
            host="localhost",
            allow_from=["*"],
            reconnect_min_delay=5.0,
            reconnect_max_delay=1.0,
        )


def test_decode_payload_json_supports_routing_fields() -> None:
    channel = MQTTChannel(_build_config(), MessageBus())

    decoded = channel._decode_payload(
        json.dumps(
            {
                "content": "hello",
                "sender_id": "sensor-1",
                "chat_id": "kitchen-speaker",
                "session_key": "mqtt:kitchen",
                "reply_topic": "devices/kitchen/replies",
                "reply_qos": 2,
                "reply_retain": True,
                "media": ["/tmp/audio.ogg"],
                "metadata": {"locale": "en-US"},
            }
        ).encode("utf-8"),
        "nanobot/kitchen/inbox",
    )

    assert decoded.content == "hello"
    assert decoded.sender_id == "sensor-1"
    assert decoded.chat_id == "kitchen-speaker"
    assert decoded.session_key == "mqtt:kitchen"
    assert decoded.reply_topic == "devices/kitchen/replies"
    assert decoded.reply_qos == 2
    assert decoded.reply_retain is True
    assert decoded.media == ["/tmp/audio.ogg"]
    assert decoded.metadata == {"locale": "en-US"}


def test_decode_payload_invalid_json_falls_back_to_text() -> None:
    channel = MQTTChannel(_build_config(payload_format="json"), MessageBus())

    decoded = channel._decode_payload(b"not json", "nanobot/device/inbox")

    assert decoded.content == "not json"
    assert decoded.metadata == {"json_parse_error": True}


def test_decode_payload_invalid_utf8_is_rejected() -> None:
    channel = MQTTChannel(_build_config(), MessageBus())

    decoded = channel._decode_payload(b"\xff\xfe", "nanobot/device/inbox")

    assert decoded.content == ""
    assert decoded.metadata == {"decode_error": True}


def test_encode_payload_json_filters_internal_metadata() -> None:
    channel = MQTTChannel(_build_config(payload_format="json"), MessageBus())

    payload = channel._encode_payload(
        OutboundMessage(
            channel="mqtt",
            chat_id="kitchen",
            content="hello",
            media=["/tmp/reply.ogg"],
            reply_to="abc123",
            metadata={
                "locale": "en-US",
                "_mqtt_reply_topic": "devices/kitchen/replies",
                "_progress": True,
            },
        )
    )

    data = _decode_json_payload(payload)
    assert data == {
        "content": "hello",
        "media": ["/tmp/reply.ogg"],
        "reply_to": "abc123",
        "metadata": {"locale": "en-US"},
    }


@pytest.mark.asyncio
async def test_handle_mqtt_message_uses_topic_vars_for_routing() -> None:
    bus = MessageBus()
    channel = MQTTChannel(_build_config(), bus)
    subscription = channel._subscriptions[0]

    await channel._handle_mqtt_message(
        subscription,
        "nanobot/kitchen/inbox",
        b'{"content": "lights on", "metadata": {"room": "kitchen"}}',
    )

    message = await bus.consume_inbound()
    assert message.sender_id == "kitchen"
    assert message.chat_id == "kitchen"
    assert message.content == "lights on"
    assert message.metadata["room"] == "kitchen"
    assert message.metadata["_mqtt_topic_vars"] == {"chat_id": "kitchen"}


@pytest.mark.asyncio
async def test_handle_mqtt_message_supports_payload_override_and_session_template() -> None:
    config = _build_config(
        subscribe_topics=[
            MQTTTopicConfig(
                topic="nanobot/{device_id}/inbox",
                session_key_template="mqtt:{device_id}:voice",
            )
        ]
    )
    bus = MessageBus()
    channel = MQTTChannel(config, bus)
    subscription = channel._subscriptions[0]

    await channel._handle_mqtt_message(
        subscription,
        "nanobot/entryway-speaker/inbox",
        json.dumps(
            {
                "content": "what time is it",
                "sender_id": "owner",
                "reply_topic": "devices/entryway-speaker/replies",
            }
        ).encode("utf-8"),
    )

    message = await bus.consume_inbound()
    assert message.sender_id == "owner"
    assert message.chat_id == "entryway-speaker"
    assert message.session_key == "mqtt:entryway-speaker:voice"
    assert message.metadata["_mqtt_reply_topic"] == "devices/entryway-speaker/replies"


@pytest.mark.asyncio
async def test_handle_mqtt_message_drops_unroutable_messages() -> None:
    bus = MessageBus()
    config = _build_config(subscribe_topics=[MQTTTopicConfig(topic="nanobot/inbox")])
    channel = MQTTChannel(config, bus)
    subscription = channel._subscriptions[0]

    await channel._handle_mqtt_message(subscription, "nanobot/inbox", b'{"content":"hi"}')

    assert bus.inbound_size == 0


def test_build_publish_topic_prefers_reply_topic_metadata() -> None:
    channel = MQTTChannel(_build_config(), MessageBus())

    topic = channel._build_publish_topic(
        OutboundMessage(
            channel="mqtt",
            chat_id="ignored",
            content="hello",
            metadata={"_mqtt_reply_topic": "devices/kitchen/replies"},
        )
    )

    assert topic == "devices/kitchen/replies"


def test_build_publish_topic_renders_topic_vars() -> None:
    config = _build_config(publish_topic_template="devices/{device_id}/outbox")
    channel = MQTTChannel(config, MessageBus())

    topic = channel._build_publish_topic(
        OutboundMessage(
            channel="mqtt",
            chat_id="fallback",
            content="hello",
            metadata={"_mqtt_topic_vars": {"device_id": "kitchen-speaker"}},
        )
    )

    assert topic == "devices/kitchen-speaker/outbox"


def test_publish_options_prefer_reply_overrides() -> None:
    channel = MQTTChannel(_build_config(publish_qos=0, retain_outbound=False), MessageBus())
    outbound_message = OutboundMessage(
        channel="mqtt",
        chat_id="kitchen",
        content="hello",
        metadata={"_mqtt_reply_qos": 2, "_mqtt_reply_retain": True},
    )

    assert channel._resolve_publish_quality_of_service(outbound_message) == 2
    assert channel._resolve_publish_retain_flag(outbound_message) is True


def test_build_tls_context_respects_tls_insecure() -> None:
    channel = MQTTChannel(
        _build_config(use_tls=True, tls_insecure=True),
        MessageBus(),
    )

    context = channel._build_tls_context()

    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_run_client_subscribes_and_publishes_birth_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr("nanobot.channels.mqtt.Client", _FakeClient)

    channel = MQTTChannel(
        _build_config(
            username="user",
            password="pass",
            client_id="nanobot-test",
            subscribe_topics=[
                MQTTTopicConfig(topic="nanobot/{chat_id}/inbox", qos=1),
                MQTTTopicConfig(topic="devices/{device_id}/requests", qos=2),
            ],
            birth_topic="nanobot/status",
            birth_payload="online",
        ),
        MessageBus(),
    )

    await channel._run_client()

    client = _FakeClient.instances[-1]
    assert client.connection_options["hostname"] == "localhost"
    assert client.connection_options["port"] == 1883
    assert client.connection_options["identifier"] == "nanobot-test"
    assert client.connection_options["username"] == "user"
    assert client.connection_options["password"] == "pass"
    assert client.subscriptions == [
        ("nanobot/+/inbox", 1),
        ("devices/+/requests", 2),
    ]
    assert client.published == [
        {
            "topic": "nanobot/status",
            "payload": "online",
            "qos": 1,
            "retain": True,
        }
    ]


@pytest.mark.asyncio
async def test_send_publishes_encoded_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr("nanobot.channels.mqtt.Client", _FakeClient)

    channel = MQTTChannel(_build_config(), MessageBus())
    channel._client = _FakeClient()

    await channel.send(
        OutboundMessage(
            channel="mqtt",
            chat_id="kitchen",
            content="hello",
            metadata={"locale": "en-US"},
        )
    )

    assert len(channel._client.published) == 1
    published = channel._client.published[0]
    assert published["topic"] == "nanobot/kitchen/outbox"
    assert published["qos"] == 1
    assert published["retain"] is False
    assert _decode_json_payload(published["payload"]) == {
        "content": "hello",
        "metadata": {"locale": "en-US"},
    }


@pytest.mark.asyncio
async def test_stop_disconnects_active_client() -> None:
    channel = MQTTChannel(_build_config(), MessageBus())
    channel._client = _FakeClient()

    await channel.stop()

    assert channel._client is None
    assert _FakeClient.instances[-1].disconnect_called is True


@pytest.mark.asyncio
async def test_start_retries_after_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = MQTTChannel(_build_config(), MessageBus())
    events: list[str] = []

    async def fake_run_client() -> None:
        events.append("run")
        raise RuntimeError("boom")

    async def fake_backoff() -> None:
        events.append("backoff")
        channel._running = False

    monkeypatch.setattr(channel, "_run_client", fake_run_client)
    monkeypatch.setattr(channel, "_exponential_backoff", fake_backoff)

    await channel.start()

    assert events == ["run", "backoff"]


@pytest.mark.asyncio
async def test_start_retries_after_clean_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = MQTTChannel(_build_config(), MessageBus())
    events: list[str] = []

    async def fake_run_client() -> None:
        events.append("run")

    async def fake_backoff() -> None:
        events.append("backoff")
        channel._running = False

    monkeypatch.setattr(channel, "_run_client", fake_run_client)
    monkeypatch.setattr(channel, "_exponential_backoff", fake_backoff)

    await channel.start()

    assert events == ["run", "backoff"]
