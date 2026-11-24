# Codie Pi Bridge Spellbook

A Raspberry Pi gives Codie a gateway to services and hardware it cannot reach directly. Codie can invoke a tool registered by the Pi, and the Pi can turn approved events from other systems into spoken notifications. Home Assistant is one example of what can sit behind the gateway.

| Feature | Example |
| --- | --- |
| Remote tool gateway | Register a narrow Codie tool that calls an approved MCP operation from the Pi. |
| Pub/Sub and LAN MQTT | Turn an external event or a message from another LAN device into speech on Codie. |
| Physical status panel | Use an Elgato Stream Deck to show status on LCD keys and send button presses to an adapter. |
| Pi web console | See connections and tool activity, and manage which tools and Home Assistant entities Codie can use. |
| Discord chat | Talk to Codie in a Discord text channel. Choose the channel and turn the connector on or off from the Pi web console. |
| OpenAI image generation | Ask for an image on demand. Discord receives an attachment, and Codie chat receives a private console link. |
| Voice announcements | A Nest doorbell ring or person event makes the active character speak. |
| Device status and control | Ask whether a light is on or turn on an allowed Hue light. |
| Door camera, on demand | Ask whether there is snow outside. Codie speaks and saves the text reply. Ask to show the front door to display the same still for 20 seconds. |
| Door-triggered porch light | A person event turns the light on. Further activity extends the three-minute timer. |

## Start here

1. Follow [Pair and install the bridge](docs/SETUP.md#1-pair-and-install-the-bridge). The basic setup needs a Raspberry Pi with systemd, Mosquitto on loopback, and a Codie device with the HTTP API enabled.
2. Read [Use the Pi as a gateway](docs/GATEWAYS.md) to connect a remote MCP, relay external Pub/Sub events, accept LAN MQTT publications, or attach an Elgato Stream Deck.
3. Optionally add [Home Assistant device access](docs/SETUP.md#2-add-home-assistant-device-access), [doorbell and porch examples](docs/SETUP.md#3-add-doorbell-announcements-and-porch-lighting), or [on-demand camera questions](docs/SETUP.md#4-enable-on-demand-camera-questions).
4. Optionally enable the [Pi web console and Discord connector](docs/CONSOLE.md).
5. Optionally enable [OpenAI image generation](docs/IMAGES.md).

## What is in the bundle

- `bridge/` has the Codie LAN executor, MQTT announcement listener, device client, snapshot worker, and optional web console and Discord connector.
- `home_assistant/` has an optional local integration and separate examples for the doorbell and porch.
- `systemd/` and `examples/` have a service and the configuration template for a fresh installation.

The reference deployment ran on Raspberry Pi OS based on Debian 13, Codie HTTP API 1.6.0, and Home Assistant Container 2026.11.2. The live reference setup exercised voice announcements, device status and control, the porch timer, and WebRTC stills. The shared bundle was generalized from that setup. See the [verification notes](docs/VERIFICATION.md) for exactly what was checked in the bundle.

## How it fits together

```text
Codie tool call -> Pi bridge -> adapter -> remote MCP / API / local service
External Pub/Sub or LAN MQTT -> Pi -> local MQTT -> Codie speech
Status source -> Pi -> Stream Deck LCD key
Stream Deck button -> Pi adapter or LAN MQTT
Home Assistant control -> Unix socket on the Pi -> Home Assistant device
Explicit camera question -> one Nest frame -> Codie vision turn -> text + speech
Explicit request to show -> same Nest frame -> temporary Codie screen display
```

The snapshot worker opens and closes a short-lived camera session per request. The image is not written to chat history. The text answer is saved as an assistant message. When display is requested, Codie clears the still after 20 seconds. The frame is sent through Codie to its configured AI provider, whose own handling is separate from Codie chat retention.

For platform details, see the [official Codie agent plugin](https://github.com/CODE27Official/codie-plugin-for-agents), [Home Assistant's Nest integration](https://www.home-assistant.io/integrations/nest/), and [Google's Nest stream documentation](https://developers.google.com/nest/device-access/traits/device/camera-live-stream).
