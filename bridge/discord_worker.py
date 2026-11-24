"""Discord chat adapter for a single authorized channel."""
import asyncio
import base64
from collections import OrderedDict
from io import BytesIO
import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands

LOG = logging.getLogger("codie-discord")
RUNTIME = Path("/run/codie-discord")
LINK = "/var/lib/codie-discord-link/device.sock"


def state(name):
    entry = RUNTIME / "state.new"
    entry.write_text(name + "\n", encoding="utf-8")
    entry.chmod(0o644)
    entry.replace(RUNTIME / "state")


async def local_call(operation, question=None, history=None, request_id=None):
    async with asyncio.timeout(70):
        reader, writer = await asyncio.open_unix_connection(LINK, limit=524288)
        try:
            request = {"operation": operation}
            if question is not None:
                request["question"] = question
            if request_id is not None:
                request["id"] = request_id
            if history:
                request["history"] = history
            writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            reply = json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
    if not reply.get("ok"):
        raise RuntimeError("Codie did not complete the request")
    return reply


class Bot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild = discord.Object(id=config["guild_id"])
        self.channel_id = config["channel_id"]
        self.user_id = None
        self.tree = app_commands.CommandTree(self)
        self.busy = asyncio.Lock()
        self.reply_history = OrderedDict()

        @self.tree.command(name="codie_status", description="Check whether the Pi can reach Codie.", guild=self.guild)
        async def status(interaction: discord.Interaction):
            await self.respond(interaction, "status")

        @self.tree.command(name="codie", description="Ask Codie a private text question.", guild=self.guild)
        async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 2, 400]):
            await self.respond(interaction, "ask", question)

    async def respond(self, interaction, operation, question=None):
        if (interaction.guild_id, interaction.channel_id, interaction.user.id) != (
            self.guild.id, self.channel_id, self.user_id
        ):
            await interaction.response.send_message("This command is restricted to the configured test channel and account.", ephemeral=True)
            return
        if self.busy.locked():
            await interaction.response.send_message("Codie is working on another request.", ephemeral=True)
            return
        async with self.busy:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await local_call(operation, question)
                answer = str(result["answer"])[:1500]
            except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as exc:
                LOG.warning("Codie request failed (%s)", type(exc).__name__)
                answer = "The Pi couldn't get a reply from Codie. Check the local connection and try again."
            await interaction.followup.send(answer, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_message(self, message):
        if (
            self.user is None or message.guild is None or message.author.bot
            or (message.guild.id, message.channel.id, message.author.id)
            != (self.guild.id, self.channel_id, self.user_id)
        ):
            return

        reference = message.reference
        history = self.reply_history.get(reference.message_id, []) if reference else []
        mention = f"<@{self.user.id}>"
        nickname_mention = f"<@!{self.user.id}>"
        if not history and not any(user.id == self.user.id for user in message.mentions):
            return
        question = message.content.replace(mention, "").replace(nickname_mention, "").strip()
        if question.startswith((",", ":")):
            question = question[1:].lstrip()
        if not 2 <= len(question) <= 400:
            return

        if self.busy.locked():
            await message.reply("I'm working on another request. Try again in a moment.", mention_author=False)
            return

        async with self.busy:
            try:
                async with message.channel.typing():
                    result = await local_call("respond", question, history, request_id=str(message.id))
            except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, discord.HTTPException) as exc:
                LOG.warning("Codie message failed (%s)", type(exc).__name__)
                await message.reply("I couldn't get a reply from Codie. Try again in a moment.", mention_author=False)
                return
        if result.get("kind") == "image":
            await self.respond_image(message, question, history, result)
            return
        answer = str(result["answer"])[:1500]
        sent = await message.reply(answer, mention_author=False)
        self.remember_reply(sent.id, history, question, answer)

    def remember_reply(self, message_id, history, question, answer):
        self.reply_history[message_id] = (history + [[question[:200], answer[:300]]])[-2:]
        if len(self.reply_history) > 64:
            self.reply_history.popitem(last=False)

    async def respond_image(self, message, question, history, result):
        try:
            if result["status"] != "queued":
                await message.reply(result["detail"], mention_author=False)
                return
            job = result["job"]
            brief = result["prompt"]
            notice = await message.reply("I’ll post the image here when it’s ready.", mention_author=False)
            async with asyncio.timeout(180):
                while result["status"] == "queued":
                    await asyncio.sleep(2)
                    result = await local_call("image_status", request_id=job)
            if result["status"] == "generated":
                with BytesIO(base64.b64decode(result["image"], validate=True)) as image:
                    sent = await message.reply(file=discord.File(image, filename="maya-image.jpg"), mention_author=False)
                    self.remember_reply(sent.id, history, question, "Generated a Discord image. Image brief: " + brief)
            else:
                await notice.edit(content="I couldn’t generate that image.")
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, discord.HTTPException) as error:
            LOG.warning("Discord image request failed (%s)", type(error).__name__)
            await message.reply("I couldn’t deliver that image.", mention_author=False)

    async def setup_hook(self):
        guild = await self.fetch_guild(self.guild.id)
        self.user_id = guild.owner_id
        await self.tree.sync(guild=self.guild)

    async def on_ready(self):
        state("online")
        LOG.info("Discord confirmed the gateway is online")

    async def on_resumed(self):
        state("online")

    async def on_disconnect(self):
        state("connecting")


async def main():
    config = json.loads((Path(os.environ["CREDENTIALS_DIRECTORY"]) / "channel.json").read_text())
    if any(type(config.get(k)) is not int or config[k] <= 0 for k in ("guild_id", "channel_id")):
        raise ValueError("Missing Discord test IDs")
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "token"
    token = credential.read_text().strip()
    if not token or any(ch.isspace() for ch in token):
        raise ValueError("Discord credential unavailable")
    state("connecting")
    async with Bot(config) as bot:
        await bot.start(token)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except (OSError, ValueError, KeyError, discord.DiscordException) as exc:
        LOG.error("Discord startup failed (%s)", type(exc).__name__)
        raise SystemExit(1)
