"""Manage the Pi's Discord connector through its private systemd socket."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile

import discord

CONFIG = Path("/etc/codie-discord/channel.json")
TOKEN = CONFIG.with_name("token")
CHANNEL_ID = re.compile(r"[1-9][0-9]{0,19}\Z")


async def allowed_channels(guild_id):
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(TOKEN.read_text().strip())
        guild = await client.fetch_guild(guild_id)
        member = await guild.fetch_member(client.user.id)
        channels = await guild.fetch_channels()
        categories = {channel.id: channel.name for channel in channels
                      if isinstance(channel, discord.CategoryChannel)}
        available = []
        for channel in channels:
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(member)
            if permissions.view_channel and permissions.send_messages and permissions.read_message_history:
                available.append({"id": str(channel.id), "name": channel.name,
                                  "category": categories.get(channel.category_id, "")})
        return sorted(available, key=lambda item: (item["category"].casefold(), item["name"].casefold()))


def channel_list(config):
    return asyncio.run(asyncio.wait_for(allowed_channels(config["guild_id"]), timeout=8))


def load_config():
    with CONFIG.open(encoding="utf-8") as source:
        return json.load(source)


def write_config(config):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG.parent, delete=False) as draft:
        path = Path(draft.name)
        try:
            json.dump(config, draft)
            draft.write("\n")
            draft.flush()
            os.fsync(draft.fileno())
            path.replace(CONFIG)
        finally:
            path.unlink(missing_ok=True)


def service(*args):
    return subprocess.run(
        ["systemctl", *args, "codie-discord.service"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False,
    ).returncode


def set_channel(channel_id):
    if not CHANNEL_ID.fullmatch(channel_id):
        return {"ok": False, "error": "invalid"}
    config = load_config()
    if not any(item["id"] == channel_id for item in channel_list(config)):
        return {"ok": False, "error": "unavailable"}
    if config["channel_id"] == int(channel_id):
        return {"ok": True}
    write_config({**config, "channel_id": int(channel_id)})
    try:
        # try-restart leaves a stopped connector stopped.
        if service("try-restart") == 0:
            return {"ok": True}
    except (OSError, subprocess.TimeoutExpired):
        pass
    write_config(config)
    service("try-restart")
    return {"ok": False, "error": "failed"}


def run(command):
    if command == "channels":
        config = load_config()
        return {"ok": True, "selected": str(config["channel_id"]), "channels": channel_list(config)}
    with (Path(os.environ["STATE_DIRECTORY"]) / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if command in {"on", "off"}:
            verb = "start" if command == "on" else "stop"
            return {"ok": service(verb) == 0}
        action, _, channel_id = command.partition(" ")
        if action == "channel":
            return set_channel(channel_id)
    return {"ok": False, "error": "invalid"}


def main():
    try:
        request = sys.stdin.buffer.readline(32)
        if not request.endswith(b"\n"):
            raise ValueError("Invalid request")
        result = run(request[:-1].decode("ascii"))
    except (discord.DiscordException, KeyError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"Discord control request failed ({type(error).__name__})", file=sys.stderr)
        result = {"ok": False, "error": "failed"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
