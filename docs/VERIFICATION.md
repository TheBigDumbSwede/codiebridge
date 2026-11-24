# Verification

The live reference installation tested Codie registration, MQTT playback, Home Assistant status and reversible controls, the chime announcement action, a physical Nest walk-up, porch-light timer expiry, and on-demand WebRTC images. The reference image flow confirmed that the picture stayed out of chat history and the answer appeared as assistant text. A live Codie test also reported the display visible and active, then closed and expired after its 20-second limit.

The distribution changes device IDs, credentials, service paths, and feature defaults. It is designed for owner-supplied values and starts with optional Home Assistant access and the camera tool disabled.

## Distribution checks

The shipped Python and YAML parsed successfully. An isolated Home Assistant 2026.11.2 container validated the merged example configuration, and systemd validated the service unit. Local fixture checks covered Codie HMAC signing, multipart image privacy settings, disabled-by-default optional tools, and the MQTT retained-message filter. The display update was also checked with fixture tests for the explicit show flag, same-image multipart payload, 20-second duration, and safe media conflict policy.

The distributable package was not paired to a second physical Codie. Follow the smoke test on a fresh installation to confirm your own devices and configuration.

The gateway guide documents extension patterns for remote MCP, cloud Pub/Sub, an authenticated Mosquitto LAN listener, and an Elgato Stream Deck. These are examples for adapting the bridge. The shipped configuration uses local MQTT, and the distribution has no configured external subscriber or physical deck.

## Smoke test in a new installation

1. Request the Codie bridge proof code.
2. Publish the labeled local announcement from the setup guide and listen for it.
3. Enable Home Assistant access, read one allowlisted entity, and test a reversible control. Try an unlisted entity and confirm access is denied.
4. Trigger a doorbell ring and person detection separately. Verify the expected speech, and watch the porch timer turn the light off after inactivity.
5. If you opted in to camera access, explicitly ask a visual question. Verify the assistant text appears in chat without an image entry. Then ask to see the picture on Codie, check that the screen displays it, and verify that it clears after 20 seconds.
6. Reboot the Pi when convenient, then check both the systemd service and any Home Assistant pieces you enabled.

A device can report that it accepted a speech or control request before a person hears speech or a physical device changes. Check the outcome at the device during setup.
