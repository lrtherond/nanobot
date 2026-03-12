# Home Assistant Assist Delegation to Nanobot over MQTT

This guide configures Home Assistant Assist to delegate requests to nanobot when
the user says `ask nanobot ...`.

The design is simple:

1. Home Assistant matches the sentence with a `conversation` trigger.
2. Home Assistant publishes the request to nanobot over MQTT.
3. Nanobot replies to a unique MQTT reply topic for that request.
4. Home Assistant waits for that reply and returns it with
   `set_conversation_response`.

## Prerequisites

- Home Assistant and nanobot must use the same MQTT broker.
- Home Assistant's MQTT integration must be configured for that broker.
- Nanobot must be running with the MQTT channel enabled.
- Nanobot should use `payloadFormat: "json"` so Home Assistant can read
  `payload_json.content` from the reply.

## Nanobot Configuration

Add this to `~/.nanobot/config.json` and merge it into the existing
`"channels"` object if needed:

```json
{
  "channels": {
    "mqtt": {
      "enabled": true,
      "host": "MQTT_BROKER_IP",
      "port": 1883,
      "subscribeTopics": [
        {
          "topic": "nanobot/{sender_id}/inbox",
          "qos": 1
        }
      ],
      "publishTopicTemplate": "nanobot/{chat_id}/outbox",
      "payloadFormat": "json",
      "allowFrom": ["home_assistant"]
    }
  }
}
```

Restart nanobot after changing `config.json`.

## Home Assistant Automation

Add the following to `automations.yaml`.

If you use packages instead of `automations.yaml`, wrap this under
`automation:`.

```yaml
- id: assist_delegate_to_nanobot
  alias: Assist Delegate To Nanobot
  mode: parallel
  max: 10
  triggers:
    - trigger: conversation
      command:
        - "ask nanobot {query}"
        - "ask nano bot {query}"

  variables:
    nanobot_request_id: >-
      {{ (trigger.device_id or 'assist') ~ '-' ~ now().strftime('%Y%m%d%H%M%S%f') }}
    nanobot_reply_topic: >-
      homeassistant/nanobot/reply/{{ nanobot_request_id }}
    nanobot_session_key: >-
      {% if trigger.user_input.conversation_id %}
        ha:{{ trigger.user_input.conversation_id }}
      {% elif trigger.device_id %}
        ha:device:{{ trigger.device_id }}
      {% else %}
        ha:request:{{ now().strftime('%Y%m%d%H%M%S%f') }}
      {% endif %}

  actions:
    - action: mqtt.publish
      data:
        topic: nanobot/home_assistant/inbox
        qos: 1
        retain: false
        payload: >-
          {{
            {
              "content": trigger.slots.query,
              "reply_topic": nanobot_reply_topic,
              "reply_qos": 1,
              "session_key": nanobot_session_key,
              "metadata": {
                "request_id": nanobot_request_id,
                "source": "home_assistant",
                "conversation_id": trigger.user_input.conversation_id,
                "device_id": trigger.device_id,
                "satellite_id": trigger.satellite_id
              }
            } | to_json
          }}

    - wait_for_trigger:
        - trigger: mqtt
          topic: "{{ nanobot_reply_topic }}"
      timeout: "00:01:00"
      continue_on_timeout: true

    - if:
        - condition: template
          value_template: "{{ wait.completed }}"
      then:
        - set_conversation_response: >-
            {% if wait.trigger.payload_json is defined and wait.trigger.payload_json.content is defined %}
              {{ wait.trigger.payload_json.content }}
            {% else %}
              {{ wait.trigger.payload }}
            {% endif %}
      else:
        - set_conversation_response: "Nanobot did not reply in time."
```

## How to Test

1. Restart nanobot.
2. Reload Home Assistant automations or restart Home Assistant.
3. Speak to your Home Assistant Voice Preview device:

   `Ask nanobot what time is it`

If everything is wired correctly, Assist will speak nanobot's reply.

## How It Works

- The `conversation` trigger captures the text after `ask nanobot` into
  `trigger.slots.query`.
- Home Assistant publishes that text to `nanobot/home_assistant/inbox`.
- The payload includes a unique `reply_topic`, so each request gets its own
  reply channel.
- The payload also includes a `session_key` so nanobot can preserve
  conversation context across follow-up requests when Home Assistant provides a
  `conversation_id`.
- Home Assistant waits for the MQTT reply and speaks the returned text.

## Practical Notes

- Every request that should be delegated still needs the prefix
  `ask nanobot ...`.
- This does not switch Assist into a permanent nanobot-only mode.
- The unique reply topic is what makes the automation safe for parallel
  requests.
- `metadata.request_id` is mainly for debugging, because the reply topic
  already correlates the request.
- If your broker requires authentication or TLS, configure those settings on
  both Home Assistant and nanobot.
