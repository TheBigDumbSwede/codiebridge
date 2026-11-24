# Set up the Codie Pi Bridge Spellbook

Use a trusted LAN and a dedicated Pi or a Pi you administer. Codie uses its paired local HTTP API on port 8081. Keep that port and Mosquitto off the public internet. The MQTT examples use a broker bound to loopback. The Home Assistant connector uses a Unix socket restricted to a local group and needs no Home Assistant access token.

## 1. Pair and install the bridge

Run these commands on the Pi from the extracted Spellbook directory. Install the prerequisites from your OS repositories. If you already run Mosquitto, inspect its listeners before using the example.

```sh
sudo apt-get install python3-paho-mqtt python3-websocket python3-pil mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
sudo groupadd --system codie-spellbook
sudo install -d -m 0755 /opt/codie-spellbook/bridge
sudo install -m 0644 bridge/*.py /opt/codie-spellbook/bridge/
sudo install -d -m 0700 /etc/codie-spellbook
sudo install -m 0600 examples/bridge.env.example /etc/codie-spellbook/bridge.env
sudo install -d -m 0755 /var/lib/codie-spellbook
sudo install -d -o root -g codie-spellbook -m 2710 /var/lib/codie-spellbook/shared
sudo ss -ltn 'sport = :1883'
```

If the group already exists, reuse it. The broker should listen on `127.0.0.1:1883` or loopback IPv6 only for this design. A fresh Mosquitto installation with its default local-only behavior is sufficient. If the broker already serves the network, use a dedicated local listener or arrange a separate broker before enabling the example. This bridge expects IPv4 loopback on port 1883.

Generate an integration UUID with `python3 -c 'from uuid import uuid4; print(uuid4())'`. Edit `/etc/codie-spellbook/bridge.env` and set `CODIE_HOST` to your Codie's LAN IPv4 address and `CODIE_INTEGRATION_ID` to the UUID. Keep the UUID stable. Enable the local API if needed, and make sure the device is signed in.

```sh
sudo sh -c 'set -a; . /etc/codie-spellbook/bridge.env; set +a; exec python3 /opt/codie-spellbook/bridge/bridge.py pair /etc/codie-spellbook/codie.json'
sudo install -m 0644 systemd/codie-spellbook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codie-spellbook
sudo journalctl -u codie-spellbook -n 30 --no-pager
```

The pair command prompts for the six-digit code displayed on Codie and saves a dedicated credential with read/write access limited to root on disk. Keep that file private. The systemd service loads it without granting the bridge access to the rest of `/etc/codie-spellbook`.

Ask the current character, “Use bridge status and tell me the proof code.” You can also use the CLI to run a test conversation. Test conversation turns use no speech or chat retention.

```sh
sudo sh -c 'set -a; . /etc/codie-spellbook/bridge.env; set +a; exec python3 /opt/codie-spellbook/bridge/bridge.py ask "Please use bridge_status and tell me the proof code."'
mosquitto_pub -h 127.0.0.1 -q 1 -t codie/bridge/announce \
  -m "$(python3 -c 'import json,uuid; print(json.dumps(dict(id=str(uuid.uuid4()), text="This is a local bridge test.")))')"
```

Expect a proof code and a separate spoken MQTT test. The bridge ignores retained messages and waits for the device to be idle before announcing. Start with the bridge on the same LAN as Codie. If the gateway cannot connect, check the IP, pairing state, port 8081, and executor log before creating a new credential.

The base setup is enough to start building your own tool or event adapter. See [Use the Pi as a gateway](GATEWAYS.md) for remote MCP, external Pub/Sub, the authenticated LAN MQTT listener, and Elgato Stream Deck integration. The remaining sections are optional Home Assistant examples.

## 2. Add Home Assistant device access

This section targets an existing **Home Assistant Container on the same Pi**. Back up your configuration and merge the example files rather than replacing your existing Compose or YAML.

1. Copy `home_assistant/custom_components/codie_home/` into your Home Assistant configuration directory under `custom_components/`.
2. Add the bind mount in `home_assistant/examples/compose-mount.example.yaml` to the Home Assistant service. Keep the host directory owned by root and accessible to the `codie-spellbook` group as above. The container must be able to create a socket in the mounted directory. The reference deployment runs the Home Assistant container as root.
3. Merge the `codie_home:` section in `home_assistant/examples/codie_home.example.yaml` into `configuration.yaml`. Fill in exact Home Assistant entity IDs for `read_entities` and `write_entities`. Write-listed devices are also readable. Keep the optional camera field commented for now.
4. Validate Home Assistant's config, then recreate just that container to apply the new mount. Check for `/var/lib/codie-spellbook/shared/device.sock` on the host.
5. Set `CODIE_ENABLE_HOME=1` in the bridge environment file and restart `codie-spellbook`. Ask for the status of one named device, then test a reversible command.

The write allowlist covers supported Matter Hue lights and switches, Roomba actions, Android TV and Cast controls, and BedJet climate commands. Individual devices can expose fewer actions. Sensors, camera status, and door events stay read-only. Nest and BedJet need their own Home Assistant integrations. This package does not install Google's Device Access or the community BedJet integration for you.

## 3. Add doorbell announcements and porch lighting

The examples use Home Assistant's Nest event entities and assume the container shares the Pi's host networking so `127.0.0.1:1883` reaches Mosquitto. Check that Paho is present in the container with `docker exec homeassistant python -c 'import paho.mqtt.publish'`.

Copy `home_assistant/codie_front_door.py` to the Home Assistant config directory. Merge the `shell_command`, `input_boolean`, and `timer` entries from the helpers example into `configuration.yaml`. Add the automation entries from `front_door_automations.yaml` to your existing automations list. Replace all three `replace_with_` entity IDs with the chime event, camera motion event, and porch light IDs from your installation. Keep only the automations and helpers you intend to use.

Test a real walk-up and a separate press of the doorbell. The voice examples announce person detections and rings. The porch automation starts on person detection, extends while active on additional person, motion, or ring events, and keeps an already-on porch light on. Generic motion by itself does not turn the light on. The three-minute duration is configured in the timer helper.

## 4. Enable on-demand camera questions

This requires an authorized, working Nest camera in Home Assistant and a Codie AI configuration that supports image input. Install `python3-aiortc` on the Pi if your doorbell uses WebRTC. RTSP cameras use Home Assistant's still-image helper.

Set `front_door_camera` to exactly one Nest `camera.*` entity in Home Assistant's `codie_home` configuration. Restart Home Assistant. Set `CODIE_ENABLE_CAMERA=1` alongside `CODIE_ENABLE_HOME=1` on the Pi, then restart `codie-spellbook`. Ask, “Please look at the front-door camera and tell me whether the porch is clear.”

Expect an acknowledgment, then a spoken follow-up and the text reply in Codie history. There is no background capture schedule. No tool can select a different camera. The bridge forwards a single frame to Codie with image-turn retention set to `none`. The bridge posts only the answer as an assistant text message for chat and speech. For WebRTC, the temporary stream closes after the first frame or a timeout.

**Show the exact still.** Say “Show me the front door.” Codie displays the same frame the character analyzes for 20 seconds. An ordinary visual question still gets a voice and text answer. It does not display the image. The still is kept out of chat history in both cases. If the display is busy with other media, the bridge leaves that session alone and returns the visual answer with a display-status note.

**Update an existing Spellbook installation.** For the display feature, replace only the installed bridge module and restart the service. The Home Assistant settings and original pairing can be reused.

```sh
sudo install -m 0644 bridge/front_camera.py /opt/codie-spellbook/bridge/front_camera.py
sudo systemctl restart codie-spellbook
```

To disable camera access, set `CODIE_ENABLE_CAMERA=0`, remove `front_door_camera` from Home Assistant, and restart both services. This also removes the camera tool from the bridge's Codie catalog.
