# OpenAI second opinions

Set up the [Pi web console](CONSOLE.md#enable-the-console) first. Codie can ask OpenAI for an independent assessment when you request a second opinion. The reply includes the upstream assessment, uncertainty, and a next check. If you ask for a critique, the Pi also sends the draft reasoning you want reviewed.

## Enable the tool

The Pi uses the same OpenAI credential file as [image generation](IMAGES.md#install-and-enable). The project key needs permission to call the Responses API. You can enable second opinions by themselves. Follow the key-file instructions once if the Pi does not already have that credential.

Set `CODIE_ENABLE_SECOND_OPINION=1` in `/etc/codie-spellbook/bridge.env`. From the spellbook checkout on the Pi, install the updated bridge files and shared credential drop-in. Keep any avatar settings in an existing drop-in.

```sh
sudo install -m 0644 bridge/*.py bridge/console.css /opt/codie-spellbook/bridge/
if [ ! -f /etc/systemd/system/codie-spellbook.service.d/openai-images.conf ]; then
  sudo install -m 0644 systemd/openai-images.conf /etc/systemd/system/codie-spellbook.service.d/
fi
sudo systemctl daemon-reload
sudo systemctl restart codie-spellbook
sudo systemctl try-restart codie-discord.service   # Only if you have installed Discord
```

In the console, choose **Codie access → OpenAI second opinion → Turn on second opinions**. If the tool is missing in an existing Codie conversation, start a new one.

## Ask for a second opinion

Say, “Get a second opinion on why a porch light automation works when I run it manually but never triggers on motion.” Codie replies in the original conversation. In Discord, mention the bot or reply to it with the mention on. If Codie’s comparison is unavailable, the Discord reply shows the upstream assessment and Codie’s initial assessment separately.

The Pi sends a focused question and relevant facts. Codie’s initial assessment stays on the Pi side for an independent request. Calls are billed to the OpenAI API project behind the key. The bridge logs the tool outcome and duration.
