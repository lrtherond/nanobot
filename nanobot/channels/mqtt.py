"""MQTT channel implementation using aiomqtt."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import string
from dataclasses import dataclass
from typing import Any

from aiomqtt import Client, MqttError, Will
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import MQTTConfig, MQTTTopicConfig

_PLACEHOLDER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class _DecodedPayload:
    """Normalized inbound MQTT payload."""

    content: str
    metadata: dict[str, Any]
    media: list[str]
    sender_id: str | None = None
    chat_id: str | None = None
    session_key: str | None = None
    reply_topic: str | None = None
    reply_qos: int | None = None
    reply_retain: bool | None = None


@dataclass(frozen=True)
class _CompiledSubscription:
    """Compiled inbound MQTT subscription definition."""

    topic_template: str
    mqtt_filter: str
    topic_regex: re.Pattern[str]
    qos: int
    session_key_template: str | None = None


class MQTTChannel(BaseChannel):
    """MQTT channel for device and automation integrations."""

    name = "mqtt"
    display_name = "MQTT"

    def __init__(self, config: MQTTConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: MQTTConfig = config
        self._client: Client | None = None
        self._subscriptions = [
            self._compile_subscription(topic_config) for topic_config in config.subscribe_topics
        ]
        self._reconnect_delay = max(
            min(self.config.reconnect_min_delay, self.config.reconnect_max_delay),
            0.1,
        )

    @classmethod
    def _compile_subscription(cls, topic_config: MQTTTopicConfig) -> _CompiledSubscription:
        """Compile a topic template into a broker filter and a regex matcher."""
        mqtt_filter, topic_regex = cls._compile_topic_template(topic_config.topic)
        return _CompiledSubscription(
            topic_template=topic_config.topic,
            mqtt_filter=mqtt_filter,
            topic_regex=topic_regex,
            qos=topic_config.qos,
            session_key_template=topic_config.session_key_template,
        )

    @staticmethod
    def _compile_topic_template(topic_template: str) -> tuple[str, re.Pattern[str]]:
        """Compile a topic template with named placeholders into MQTT and regex forms."""
        if not topic_template.strip():
            raise ValueError("MQTT topic template cannot be empty")
        if "+" in topic_template or "#" in topic_template:
            raise ValueError(
                "MQTT topic templates must use named placeholders like "
                "'nanobot/{chat_id}/inbox' instead of '+' or '#'"
            )

        formatter = string.Formatter()
        field_names: set[str] = set()
        mqtt_filter_parts: list[str] = []
        pattern_parts = ["^"]

        for literal_text, field_name, format_spec, conversion in formatter.parse(topic_template):
            if literal_text:
                mqtt_filter_parts.append(literal_text)
                pattern_parts.append(re.escape(literal_text))

            if field_name is None:
                continue

            if format_spec or conversion:
                raise ValueError(
                    f"MQTT topic template '{topic_template}' cannot use format modifiers"
                )
            if not _PLACEHOLDER_RE.fullmatch(field_name):
                raise ValueError(f"MQTT topic template placeholder '{field_name}' is invalid")
            if field_name in field_names:
                raise ValueError(f"MQTT topic template placeholder '{field_name}' is duplicated")

            field_names.add(field_name)
            mqtt_filter_parts.append("+")
            pattern_parts.append(f"(?P<{field_name}>[^/]+)")

        pattern_parts.append("$")
        return "".join(mqtt_filter_parts), re.compile("".join(pattern_parts))

    @staticmethod
    def _coerce_payload_bytes(payload: bytes | bytearray | memoryview | str) -> bytes:
        """Normalize MQTT payload values to bytes."""
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return bytes(payload)

    @staticmethod
    def _coerce_string(value: Any) -> str | None:
        """Return a stripped string value when available."""
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _coerce_media(value: Any) -> list[str]:
        """Normalize a payload media list."""
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _coerce_quality_of_service(value: Any) -> int | None:
        """Validate an MQTT quality-of-service override."""
        return value if isinstance(value, int) and value in (0, 1, 2) else None

    def _build_tls_context(self) -> ssl.SSLContext | None:
        """Build an SSL context when TLS is enabled."""
        if not self.config.use_tls:
            return None

        context = ssl.create_default_context()
        if self.config.tls_ca_certs:
            context.load_verify_locations(cafile=self.config.tls_ca_certs)
        if self.config.tls_insecure:
            logger.warning("MQTT TLS certificate verification is disabled")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # nosec B323
        return context

    def _build_will(self) -> Will | None:
        """Create the broker last-will message."""
        if not self.config.will.enabled:
            return None
        return Will(
            topic=self.config.will.topic,
            payload=self.config.will.payload,
            qos=self.config.will.qos,
            retain=self.config.will.retain,
        )

    def _decode_payload(
        self, payload: bytes | bytearray | memoryview | str, topic: str
    ) -> _DecodedPayload:
        """Decode an inbound MQTT payload according to channel settings."""
        try:
            text = self._coerce_payload_bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("MQTT payload from '{}' is not valid UTF-8", topic)
            return _DecodedPayload(
                content="",
                metadata={"decode_error": True},
                media=[],
            )

        if self.config.payload_format == "text":
            return _DecodedPayload(content=text, metadata={}, media=[])

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _DecodedPayload(
                content=text,
                metadata={"json_parse_error": True},
                media=[],
            )

        if not isinstance(data, dict):
            return _DecodedPayload(content=str(data), metadata={}, media=[])

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {"raw_metadata": metadata}

        content_value = data.get("content", data.get("text", ""))
        content = content_value if isinstance(content_value, str) else str(content_value)

        reply_topic = self._coerce_string(data.get("reply_topic")) or self._coerce_string(
            data.get("response_topic")
        )

        return _DecodedPayload(
            content=content,
            metadata=metadata,
            media=self._coerce_media(data.get("media")),
            sender_id=self._coerce_string(data.get("sender_id")),
            chat_id=self._coerce_string(data.get("chat_id")),
            session_key=self._coerce_string(data.get("session_key")),
            reply_topic=reply_topic,
            reply_qos=self._coerce_quality_of_service(data.get("reply_qos")),
            reply_retain=(
                data.get("reply_retain") if isinstance(data.get("reply_retain"), bool) else None
            ),
        )

    def _encode_payload(self, outbound_message: OutboundMessage) -> bytes:
        """Encode an outbound message according to channel settings."""
        if self.config.payload_format == "text":
            return (outbound_message.content or "").encode("utf-8")

        payload: dict[str, Any] = {"content": outbound_message.content or ""}

        if outbound_message.media:
            payload["media"] = outbound_message.media
        if outbound_message.reply_to:
            payload["reply_to"] = outbound_message.reply_to

        if outbound_message.metadata:
            # Routing keys stay internal to nanobot and must not leak into MQTT payloads.
            filtered = {
                key: value
                for key, value in outbound_message.metadata.items()
                if not key.startswith("_")
            }
            if filtered:
                payload["metadata"] = filtered

        return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    @staticmethod
    def _resolve_chat_id(decoded: _DecodedPayload, topic_vars: dict[str, str]) -> str | None:
        """Resolve the chat id for an inbound message."""
        if decoded.chat_id:
            return decoded.chat_id
        for key in ("chat_id", "device_id", "sender_id"):
            if key in topic_vars and topic_vars[key]:
                return topic_vars[key]
        if len(topic_vars) == 1:
            return next(iter(topic_vars.values()))
        return None

    @staticmethod
    def _resolve_sender_id(
        decoded: _DecodedPayload, topic_vars: dict[str, str], chat_id: str
    ) -> str:
        """Resolve the sender id for an inbound message."""
        if decoded.sender_id:
            return decoded.sender_id
        if "sender_id" in topic_vars and topic_vars["sender_id"]:
            return topic_vars["sender_id"]
        return chat_id

    @staticmethod
    def _render_template(template: str, variables: dict[str, str]) -> str:
        """Render a string template with strict variable checking."""
        try:
            return template.format_map(variables)
        except KeyError as error:
            missing = error.args[0]
            raise ValueError(
                f"Template '{template}' references missing MQTT variable '{missing}'"
            ) from error

    def _resolve_session_key(
        self,
        subscription: _CompiledSubscription,
        decoded: _DecodedPayload,
        topic_vars: dict[str, str],
        chat_id: str,
        sender_id: str,
    ) -> str | None:
        """Resolve an optional session key override."""
        if decoded.session_key:
            return decoded.session_key
        if not subscription.session_key_template:
            return None

        template_vars = {
            **topic_vars,
            "chat_id": chat_id,
            "sender_id": sender_id,
        }
        return self._render_template(subscription.session_key_template, template_vars)

    @staticmethod
    def _build_publish_template_variables(
        outbound_message: OutboundMessage,
    ) -> dict[str, str]:
        """Build publish template variables from an outbound message."""
        template_variables = {"chat_id": outbound_message.chat_id}
        if not outbound_message.metadata:
            return template_variables

        topic_variables = outbound_message.metadata.get("_mqtt_topic_vars")
        if isinstance(topic_variables, dict):
            template_variables.update(
                {
                    str(key): str(value)
                    for key, value in topic_variables.items()
                    if value is not None
                }
            )

        sender_id = outbound_message.metadata.get("_mqtt_sender_id")
        if isinstance(sender_id, str) and sender_id:
            template_variables.setdefault("sender_id", sender_id)

        return template_variables

    def _build_publish_topic(self, outbound_message: OutboundMessage) -> str:
        """Resolve the outbound topic for a reply."""
        if outbound_message.metadata:
            reply_topic = outbound_message.metadata.get("_mqtt_reply_topic")
            if isinstance(reply_topic, str) and reply_topic:
                return reply_topic

        return self._render_template(
            self.config.publish_topic_template,
            self._build_publish_template_variables(outbound_message),
        )

    def _resolve_publish_quality_of_service(self, outbound_message: OutboundMessage) -> int:
        """Resolve the outbound QoS for a reply."""
        if outbound_message.metadata:
            reply_qos = outbound_message.metadata.get("_mqtt_reply_qos")
            if isinstance(reply_qos, int) and reply_qos in (0, 1, 2):
                return reply_qos
        return self.config.publish_qos

    def _resolve_publish_retain_flag(self, outbound_message: OutboundMessage) -> bool:
        """Resolve the outbound retain flag for a reply."""
        if outbound_message.metadata:
            reply_retain = outbound_message.metadata.get("_mqtt_reply_retain")
            if isinstance(reply_retain, bool):
                return reply_retain
        return self.config.retain_outbound

    async def _publish_birth_message(self, client: Client) -> None:
        """Publish the configured birth message once the broker connection is up."""
        if not self.config.birth_enabled:
            return
        await client.publish(
            topic=self.config.birth_topic,
            payload=self.config.birth_payload,
            qos=self.config.birth_qos,
            retain=self.config.birth_retain,
        )

    async def _handle_mqtt_message(
        self, subscription: _CompiledSubscription, topic: str, payload: Any
    ) -> None:
        """Process a single inbound MQTT message."""
        topic_match = subscription.topic_regex.fullmatch(topic)
        if not topic_match:
            logger.debug("MQTT message from unmatched topic '{}'", topic)
            return

        topic_vars = {key: value for key, value in topic_match.groupdict().items() if value}
        decoded = self._decode_payload(payload, topic)
        chat_id = self._resolve_chat_id(decoded, topic_vars)
        if not chat_id:
            logger.warning(
                "MQTT message from '{}' did not resolve a chat_id; dropping message",
                topic,
            )
            return

        sender_id = self._resolve_sender_id(decoded, topic_vars, chat_id)

        try:
            session_key = self._resolve_session_key(
                subscription,
                decoded,
                topic_vars,
                chat_id,
                sender_id,
            )
        except ValueError as error:
            logger.warning("{}", error)
            session_key = None

        metadata = dict(decoded.metadata)
        metadata["_mqtt_inbound_topic"] = topic
        metadata["_mqtt_topic_vars"] = topic_vars
        metadata["_mqtt_sender_id"] = sender_id
        if decoded.reply_topic:
            metadata["_mqtt_reply_topic"] = decoded.reply_topic
        if decoded.reply_qos is not None:
            metadata["_mqtt_reply_qos"] = decoded.reply_qos
        if decoded.reply_retain is not None:
            metadata["_mqtt_reply_retain"] = decoded.reply_retain

        if not decoded.content and not decoded.media:
            logger.debug("Ignoring empty MQTT message from '{}'", topic)
            return

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=decoded.content,
            media=decoded.media,
            metadata=metadata,
            session_key=session_key,
        )

    async def _message_loop(self, client: Client) -> None:
        """Consume inbound MQTT messages until the client stops."""
        async for message in client.messages:
            if not self._running:
                break

            topic = str(message.topic)
            for subscription in self._subscriptions:
                if subscription.topic_regex.fullmatch(topic):
                    await self._handle_mqtt_message(
                        subscription,
                        topic,
                        message.payload,
                    )
                    break
            else:
                logger.debug("MQTT message from unmatched topic '{}'", topic)

    async def _exponential_backoff(self) -> None:
        """Sleep for the current reconnect delay, then grow it."""
        logger.info("Reconnecting to MQTT in {:.1f} seconds...", self._reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(
            max(self._reconnect_delay * 2, self.config.reconnect_min_delay),
            self.config.reconnect_max_delay,
        )

    async def _run_client(self) -> None:
        """Connect to the broker, subscribe, and process inbound messages."""
        client_options: dict[str, Any] = {
            "hostname": self.config.host,
            "port": self.config.port,
            "keepalive": self.config.keepalive,
            "clean_session": self.config.clean_session,
        }

        if self.config.client_id:
            client_options["identifier"] = self.config.client_id
        if self.config.username:
            client_options["username"] = self.config.username
        if self.config.password:
            client_options["password"] = self.config.password

        tls_context = self._build_tls_context()
        if tls_context is not None:
            client_options["tls_context"] = tls_context

        will = self._build_will()
        if will is not None:
            client_options["will"] = will

        try:
            async with Client(**client_options) as client:
                self._client = client
                self._reconnect_delay = self.config.reconnect_min_delay
                logger.info("MQTT connected to {}:{}", self.config.host, self.config.port)

                for subscription in self._subscriptions:
                    await client.subscribe(subscription.mqtt_filter, qos=subscription.qos)
                    logger.debug(
                        "MQTT subscribed to '{}' (QoS {})",
                        subscription.mqtt_filter,
                        subscription.qos,
                    )

                await self._publish_birth_message(client)
                await self._message_loop(client)
        finally:
            self._client = None

    async def start(self) -> None:
        """Start the MQTT reconnect loop."""
        if not self.config.host:
            logger.error("MQTT broker host not configured")
            return

        self._running = True
        logger.info("Starting MQTT channel ({}:{})", self.config.host, self.config.port)

        while self._running:
            try:
                await self._run_client()
                if self._running:
                    logger.warning("MQTT connection closed unexpectedly")
                    await self._exponential_backoff()
            except MqttError as error:
                if not self._running:
                    break
                logger.warning("MQTT connection error: {}", error)
                await self._exponential_backoff()
            except Exception as error:
                if not self._running:
                    break
                logger.exception("Unexpected MQTT error: {}", error)
                await self._exponential_backoff()

    async def stop(self) -> None:
        """Stop the MQTT client."""
        self._running = False
        disconnect = getattr(self._client, "disconnect", None)
        if disconnect is not None:
            try:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as error:
                logger.warning("MQTT disconnect failed: {}", error)
        self._client = None

    async def send(self, outbound_message: OutboundMessage) -> None:
        """Publish an outbound message to MQTT."""
        if not self._client:
            logger.warning("MQTT client not connected, cannot send message")
            return

        topic = self._build_publish_topic(outbound_message)
        payload = self._encode_payload(outbound_message)

        await self._client.publish(
            topic=topic,
            payload=payload,
            qos=self._resolve_publish_quality_of_service(outbound_message),
            retain=self._resolve_publish_retain_flag(outbound_message),
        )
        logger.debug(
            "MQTT published to '{}' ({} bytes)",
            topic,
            len(payload),
        )
