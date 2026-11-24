# Pi web console and Discord

Start with the [basic spellbook installation](SETUP.md#1-pair-and-install-the-bridge). The console and Discord connector both run on the Pi. Use a web browser on the configured local network. Keep the console port off the public internet.

## Enable the console

If updating an existing installation, install the updated bridge code and main unit from the checkout. Keep your existing pairing credential and environment file.

```sh
sudo install -m 0644 bridge/*.py bridge/console.css /opt/codie-spellbook/bridge/
sudo install -m 0644 systemd/codie-spellbook.service /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/codie-spellbook.service.d
sudo install -m 0644 systemd/console-tls.conf /etc/systemd/system/codie-spellbook.service.d/
```

Set these values in `/etc/codie-spellbook/bridge.env`, using your Pi’s IPv4 address and local subnet. Reserve that address on your router so the browser URL and certificate stay valid.

```ini
CODIE_ENABLE_CONSOLE=1
CODIE_CONSOLE_LAN=192.168.1.30
CODIE_CONSOLE_NETWORK=192.168.1.0/24
```

Put a certificate and matching private key for that address in `/etc/codie-spellbook/console.crt` and `/etc/codie-spellbook/console.key`. If you do not already issue certificates for your LAN, create a self-signed certificate for this Pi. Use the same address as above.

```sh
PI_IP=192.168.1.30
sudo sh -c 'umask 077; exec openssl req -x509 -newkey rsa:3072 -nodes -days 365 -sha256 \
  -subj "/CN=Codie Bridge LAN Console" -addext "subjectAltName=IP:$1" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" \
  -keyout /etc/codie-spellbook/console.key -out /etc/codie-spellbook/console.crt' sh "$PI_IP"
```

Generate the console password on the Pi. This prints the new password once and saves only its digest. Store the password in your password manager.

```sh
sudo sh -c 'umask 077; python3 -' <<'PY'
import hashlib
import secrets
from pathlib import Path

password = secrets.token_urlsafe(30)
Path("/etc/codie-spellbook/console-password.sha256").write_text(
    hashlib.sha256(password.encode()).hexdigest() + "\n"
)
print(password)
PY
sudo systemctl daemon-reload
sudo systemctl restart codie-spellbook
```

For a self-signed certificate, securely copy only the public `console.crt` to the browsing device and trust that specific certificate there. Confirm its SHA-256 fingerprint matches `sudo openssl x509 -in /etc/codie-spellbook/console.crt -noout -fingerprint -sha256` on the Pi. Then visit `https://PI_IP:8788/` and sign in as `codie`.

Use **Codie access** to block tools or make individual Home Assistant entities read-only or hidden. Existing Home Assistant access rules still cap what the bridge can do. The front door snapshot has its own tool setting. **Activity** displays tool name, time, outcome, and duration for calls handled by the bridge after the console was enabled.

## Add Discord chat and the channel picker

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Install it in a server you own with the `bot` and `applications.commands` scopes. Give it **View Channel**, **Send Messages**, and **Read Message History** in the text channels you want to use.
2. With Discord Developer Mode enabled, copy the server ID and one initial text channel ID. Replace the two example IDs before running the commands. Enter the bot token when the editor opens, and keep both files readable only by root.
3. Set `CODIE_ENABLE_DISCORD=1` in `/etc/codie-spellbook/bridge.env` before restarting the service.

```sh
sudo apt-get install python3-discord
getent group codie-discord-link >/dev/null || sudo groupadd --system codie-discord-link
getent group codie-console-control >/dev/null || sudo groupadd --system codie-console-control
sudo install -d -m 0700 /etc/codie-discord
sudo install -d -o root -g codie-discord-link -m 2770 /var/lib/codie-discord-link
sudo install -m 0600 /dev/null /etc/codie-discord/token
sudoedit /etc/codie-discord/token
sudo sh -c 'umask 077; cat > /etc/codie-discord/channel.json' <<'JSON'
{"guild_id": 111111111111111111, "channel_id": 222222222222222222}
JSON
sudo install -m 0644 systemd/discord-groups.conf /etc/systemd/system/codie-spellbook.service.d/
sudo install -m 0644 systemd/codie-discord.service systemd/codie-console-discord.socket \
  systemd/codie-console-discord@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart codie-spellbook
sudo systemctl enable --now codie-console-discord.socket
```

The token and channel creation commands above are for a fresh install. Keep both files when upgrading an existing Discord connector.

Open the console and use **Connections → Discord → Choose channel**. The list shows text channels in the configured server where the bot can read and send messages. Save one channel, then use **Turn on Discord** when you want the connector active. Saving a new channel restarts an active connector and leaves a stopped connector off.

In the chosen channel, mention the bot. Use Discord’s **Reply** with the mention left on for follow-ups. Only the server owner can invoke the connector. The bot’s normal replies are visible to people who can read that channel. Discord chat turns are text-only, with Codie tools, speech and chat retention disabled. Anyone with the console password can change its settings.
