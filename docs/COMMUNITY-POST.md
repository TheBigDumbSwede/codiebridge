# Codie Pi Bridge Spellbook

This community Spellbook uses a Raspberry Pi as a gateway between Codie and the services or hardware you want to connect. The Pi can expose a carefully scoped MCP tool to Codie or deliver external Pub/Sub and MQTT events as voice announcements.

The gateway guide explains remote MCP adapters, sending announcements from another device on your LAN, and using an Elgato Stream Deck as a physical status and control panel. Home Assistant is a worked example that covers explicitly allowed devices, doorbell events, and temporary front-door stills.

The download includes the Python bridge, a systemd service, and setup instructions for Codie HTTP API 1.6.0. Start with the MQTT speech test, then add the tools or event sources you want.
