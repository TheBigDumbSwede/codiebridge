# Use the Pi as a gateway

The Pi is an external tool executor for Codie. It can reach services using the credentials, protocols, and network routes available on the Pi. Codie only sees the tools you register and the results those tools return. Home Assistant is one example of a system attached to this gateway.

```text
Question or command
  Codie -> paired LAN tool channel -> Pi adapter -> remote MCP or another service
  Codie <- compact tool result <- Pi adapter <- service result

Event notification
  External topic -> Pi subscriber -> validated local MQTT message -> Codie speech
```

The bundled code implements the paired tool channel and local MQTT speech path. The remote MCP client and external topic subscriber are extension examples. Add them only for services you operate or are authorized to use. These examples require no particular home automation platform.

## Connect a remote MCP server

1. **Design a narrow Codie tool.** For example, expose `service_health` for one internal service. Accept a validated environment name and return a short result such as availability, a timestamp, and a bounded summary. A friendly tool name and a clear description help the character choose the right tool. Avoid giving Codie a generic tool that accepts a server URL, arbitrary MCP tool name, or shell command.
2. **Connect from the Pi.** Configure the MCP endpoint and its credentials on the Pi, using its required transport and authentication. A remote Streamable HTTP MCP commonly uses HTTPS and an access token or OAuth. A local stdio MCP typically starts as a child process. Make sure the systemd service can reach the endpoint or run the stdio command under its service user. Keep credentials outside source, tool arguments, and tool output. The service's existing `LoadCredential=` pattern can also load a dedicated read-only MCP secret.
3. **Choose an upstream method.** Use MCP tool discovery to check the exact tool name and input/output contracts. Keep a mapping from each public Codie tool to one known upstream operation. If the upstream uses asynchronous jobs, poll within the Codie tool deadline. Return an explicit pending or unverified result if the deadline arrives before evidence.
4. **Register and dispatch it.** In [`bridge/bridge.py`](../bridge/bridge.py), add a tool definition to the catalog in `tool_loop`, using the shipped `bridge_status` or `home_device_status` declarations as examples. Add a matching `invoke` branch that validates `call["arguments"]`, calls the adapter, and returns its result. The bridge sends the reply with the same `invocation_id`. It uses the current catalog revision when registering, so bump the individual tool version when you change its contract. Set the risk and confirmation fields to fit the action. Start with a read-only operation.
5. **Keep the result small.** Normalize the MCP response to your declared `result_schema`. Return specific findings and indicate when data is stale, missing, or incomplete. Upstream text is evidence, not instructions. Strip secrets and large raw data from anything sent back to Codie.

### Adapter sketch

This example uses a hypothetical MCP tool named `get_service_health` with a structured object containing `status` and `summary`. Its async client follows the [MCP Python SDK client interface](https://py.sdk.modelcontextprotocol.io/client/). In a real integration, create and authenticate the client with the server's documented transport, then open it with `async with`. Keep the endpoint and the tool name in configuration or source controlled by the operator.

```python
import asyncio

ENVIRONMENTS = {"test", "production"}

async def service_health(client, arguments):
    environment = arguments.get("environment")
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        raise ValueError("Choose test or production")

    async with asyncio.timeout(8):
        response = await client.call_tool(
            "get_service_health", {"environment": environment}
        )
    data = response.structured_content
    if response.is_error or not isinstance(data, dict):
        return {
            "environment": environment,
            "status": "unverified",
            "summary": "The upstream service did not return usable evidence.",
        }

    status = data.get("status")
    summary = data.get("summary")
    if not isinstance(status, str) or not isinstance(summary, str):
        raise ValueError("Unexpected upstream result")
    return {
        "environment": environment,
        "status": status[:32],
        "summary": summary[:300],
    }
```

The bundled executor is synchronous. For a low-volume extension, call a bounded async wrapper with `asyncio.run()` from its dedicated dispatch thread and handle timeout or transport errors as failed tool calls with a short reason. If you maintain a long-lived MCP connection, give it a dedicated event-loop thread and keep the executor's wait bounded. For server-specific OAuth or static authentication, see the [SDK transport guide](https://py.sdk.modelcontextprotocol.io/client/transports/).

**Test the whole path.** Run the adapter against a test environment, then ask Codie for the same result by voice. Verify the upstream request used the expected target, the data matched, and an outage of the upstream adapter produces a clear failure. Test an invalid environment to confirm it is rejected before making a network call.

## Subscribe to events and let Codie speak

The announcement bridge already listens on the Pi's loopback Mosquitto broker, using topic `codie/bridge/announce`. It expects UTF-8 JSON with a UUID `id` and a `text` string of at most 400 characters. It ignores retained packets and keeps the in-memory queue bounded. A local test can use any approved event text.

```sh
mosquitto_pub -h 127.0.0.1 -q 1 -t codie/bridge/announce \
  -m '{"id":"3d65c410-7184-4d0d-966c-b333edbaa12a","text":"Test environment backup finished."}'
```

The UUID in this example is for a single test. Use a fresh UUID for a new event. For a redelivery of the same upstream event, keep the same ID.

To use an **external MQTT broker**, run a separate Pi subscriber connected to that broker with TLS and narrowly scoped credentials. Subscribe to specific topics. Validate and translate permitted events into short speech, then publish them to loopback Mosquitto. Keep the loopback broker private. For a topic you publish to from Codie, use a dedicated Codie tool with a fixed topic and bounded payload.

To use **Google Cloud Pub/Sub**, grant the adapter subscriber access on a dedicated pull subscription. The Pi pulls from the subscription and translates approved events into the same local MQTT payload. Acknowledge the cloud event after the handoff succeeds under the delivery guarantees you chose. Pub/Sub can redeliver, so derive a stable announcement UUID from the subscription and upstream event ID, and deduplicate at the adapter before injecting into MQTT. See [Google Cloud subscriber best practices](https://docs.cloud.google.com/pubsub/docs/subscribe-best-practices).

The shipped MQTT speech path is best-effort. Broker acceptance alone does not prove Codie spoke, and a Pi restart can lose a queued announcement. For important alerts, use a durable local queue or outbox and record the Codie utterance's terminal state. Filter duplicates, expired events, and bursts before queueing speech. Choose an explicit text template for each event type, and keep raw messages, personal data, and media out of automatic speech unless that is part of your design.

## Send announcements from another device on your LAN

The stock broker listens on loopback. A phone, PC, microcontroller, or Stream Deck host on the same LAN cannot reach that listener directly. To let a trusted LAN device publish, keep the working loopback listener for the bridge and add a separate authenticated TLS listener on the **same** broker. Messages published to the authorized topic on the LAN listener reach the bridge's subscription on loopback.

For Mosquitto 2.0, a merged configuration can look like this. Replace `PI_LAN_IPV4` and supply actual certificate, key, password, and ACL files. Keep any existing persistence and logging settings. If your broker already declares a loopback listener, reuse it instead of creating a duplicate. For later Mosquitto versions, check the current authentication plugin guidance. [Mosquitto broker configuration](https://mosquitto.org/man/mosquitto-conf-5.html)

```conf
per_listener_settings true

listener 1883 127.0.0.1
allow_anonymous true

listener 8883 PI_LAN_IPV4
allow_anonymous false
certfile /etc/mosquitto/certs/bridge-fullchain.pem
keyfile /etc/mosquitto/certs/bridge-key.pem
password_file /etc/mosquitto/codie-lan.passwd
acl_file /etc/mosquitto/codie-lan.acl
```

Create a dedicated user with `mosquitto_passwd` and let the broker read the certificate, key, password, and ACL files. Use a certificate for the Pi's DNS name and configure the publishing device to trust the issuing CA. Restrict access to your trusted LAN or private VPN and leave router port forwarding disabled.

A write-only ACL for a publishing device is enough. Give each publisher its own identity if you need to revoke or audit them individually.

```text
user codie_panel
topic write codie/bridge/announce
```

On the publishing device, configure its MQTT client with the Pi's DNS name, port `8883`, TLS verification, and the dedicated login. Publish to `codie/bridge/announce` with QoS 1, retain off, and the same UUID/text JSON shown above. A publish over this path speaks the supplied text. It does not start a model conversation, invoke an external tool, or create a chat record.

Test with a harmless, labeled message. Confirm that the allowed credential can publish and that a credential without the topic permission cannot. Then listen for the announcement or look for `playback completed` in `journalctl -u codie-spellbook`. Never copy the Codie pairing credential onto a sensor or button panel.

## Attach an Elgato Stream Deck

A Stream Deck with LCD keys can work as a status and control panel. Connect a supported deck to a USB data port on the Pi and drive it with the [Python Stream Deck library](https://github.com/abcminiuser/python-elgato-streamdeck), HIDAPI, and Pillow. On Linux, give the deck service user access to the specific HID device using the library's udev guidance. Elgato's desktop application is not needed for this Python route. Check that the library supports the exact deck revision before buying.

- **Status going to the deck.** Have a small Pi process read a trusted status source, render a short label or icon, and call `set_key_image()`. Use a timestamp so a cached result can go gray when it is stale. Only display fields the operator chose to expose at that physical location.
- **Button presses going back to the bridge.** Use `set_key_callback()` and handle key-down only. Queue work from the callback so a network request can't block key input. A benign first key can publish a labeled announcement to local MQTT. More advanced keys can call a specific adapter to refresh a read-only status or use a separate, explicitly authorized home-control route.
- **A deck attached to a LAN computer.** Run the USB driver next to the deck on that computer. Send approved announcement events to the Pi using the authenticated LAN listener above. If the deck also reads state, use a separate status topic and grant only the required read access. The Spellbook's existing announcement topic is for one-way speech.

The following sketch assumes you have opened a supported deck and supplied a small thread-safe work queue. The image and callback methods follow the [Stream Deck Python examples](https://python-elgato-streamdeck.readthedocs.io/en/stable/examples/basic_asyncio.html).

```python
from queue import Full
from PIL import Image, ImageDraw
from StreamDeck.ImageHelpers import PILHelper

def draw_status(deck, key, label, color):
    image = Image.new("RGB", deck.key_image_format()["size"], color)
    ImageDraw.Draw(image).text((5, 5), label, fill="white")
    deck.set_key_image(key, PILHelper.to_native_key_format(deck, image))

def install_key_handler(deck, jobs):
    def key_changed(device, key, pressed):
        if pressed and key == 0:
            try:
                jobs.put_nowait("announce_test")
            except Full:
                return

    deck.set_key_callback(key_changed)
```

In the worker, map `announce_test` to a predefined MQTT message with a fresh UUID. Test the button with that harmless message before connecting it to another service. The Spellbook does not ship a Stream Deck driver, a cloud subscriber, or a polling dashboard.

## Other gateways

Use the same tool adapter pattern for an authorized REST API or a local command with a fixed argument contract. Use the event adapter pattern for a webhook receiver or another message broker. Keep credentials and event sources on the Pi, and give each Codie tool only the operations it needs.
