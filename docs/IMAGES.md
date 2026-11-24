# OpenAI image generation

Set up the [Pi web console](CONSOLE.md#enable-the-console) first. The Pi sends an image brief to the OpenAI Images API. Image generation is billed to the OpenAI API project you supply.

## Install and enable

Create a dedicated OpenAI project key with permission to make image requests. Save the key as the only line in `/etc/codie-spellbook/openai-api-key`, owned by root and readable only by root. On a fresh installation, create the file with `sudo install -m 0600 /dev/null /etc/codie-spellbook/openai-api-key`, then enter the key in a local editor with `sudoedit /etc/codie-spellbook/openai-api-key`.

Set `CODIE_ENABLE_IMAGES=1` in `/etc/codie-spellbook/bridge.env`, then run the following commands on the Pi from the spellbook checkout.

```sh
sudo install -m 0644 bridge/*.py bridge/console.css /opt/codie-spellbook/bridge/
sudo install -m 0644 systemd/openai-images.conf /etc/systemd/system/codie-spellbook.service.d/
sudo systemctl daemon-reload
sudo systemctl restart codie-spellbook
```

In the web console, open **Codie access → OpenAI image generation** and choose **Turn on image generation**. The switch starts off. Codie may need a new conversation to see a tool added to its catalog.

If Discord is already installed, restart the Discord connector if it is currently running, so it loads the updated adapter.

```sh
sudo systemctl try-restart codie-discord
```

## Request and delivery

- On **Codie**, say “Make me a picture of a moonlit pond and show it on your screen.” The explicit screen request sends the picture to Codie’s physical display when it’s ready.
- For other **Codie** image requests, the reply includes an authenticated link to the result on the Pi. Generated chat images are held in memory for up to an hour or until the bridge restarts.
- In **Discord**, mention the bot and ask in your own words. For example, “Could you make a photorealistic portrait of yourself in a winter observatory?” The bot always delivers the image as an attachment in that channel. Use Discord’s Reply to ask for a variation. Requests to describe an image stay text-only.

The bridge allows five accepted image requests per day across these sources. The count resets at midnight in the Pi’s time zone. The console records the queue and generation or display outcomes without saving the image brief.

## Optional Maya references

To let Maya appear in pictures, supply your own reference JPEGs. The bridge can load up to five views named `maya-face.jpg`, `maya-outfit.jpg`, `maya-full.jpg`, `maya-angle.jpg`, and `maya-front.jpg`. Put whichever views you have in `/etc/codie-spellbook/` with mode 0600. Use small JPEGs so they fit in systemd’s credential storage.

For each file you install, add a matching credential line to the existing `openai-images.conf` drop-in. For example, for a face reference:

```ini
LoadCredential=maya-face.jpg:/etc/codie-spellbook/maya-face.jpg
```

Run `sudo systemctl daemon-reload` and restart `codie-spellbook`. The Pi sends the reference images to OpenAI when a request asks to include Maya. Keep your reference files and credentials on your Pi.
