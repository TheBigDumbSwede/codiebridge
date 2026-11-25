"""Owner console for the Pi's Codie bridge."""

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network, ip_address
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit

PORT = 8788
DISCORD_CONTROL = "/run/codie-console-discord.sock"
DISCORD_CHANNEL_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
IMAGE_TOOL = "generate_image"
OPINION_TOOL = "second_opinion"
OPTIONAL_TOOLS = {
    IMAGE_TOOL: ("image generation", " · Five requests per day"),
    OPINION_TOOL: ("second opinions", ""),
}
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\Z")
LABELS = {
    "bridge_status": "Bridge status",
    "home_device_status": "Home Assistant status",
    "home_device_control": "Home Assistant control",
    "front_door_snapshot": "Front door snapshot",
    IMAGE_TOOL: "OpenAI image generation",
    OPINION_TOOL: "OpenAI second opinion",
}
MODES = {"default": "Existing access", "read": "Read only", "hidden": "Hidden"}
PAGE_SIZE = 10


def discord_request(command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(36)
            connection.connect(DISCORD_CONTROL)
            connection.sendall(command.encode("ascii") + b"\n")
            with connection.makefile("rb") as response:
                return json.loads(response.readline(512 * 1024))
    except (OSError, ValueError):
        return {"ok": False, "error": "failed"}


class Console:
    def __init__(self, directory, catalog, home_status=None):
        self.db = Path(directory) / "console.sqlite3"
        self.tools = {entry["name"]: entry for entry in catalog}
        self.home_status = home_status
        self.image_worker = None
        self.opinion = None
        self.csrf = secrets.token_urlsafe(32)
        self.executor_online = False
        self.mqtt_online = False
        self.css = Path(__file__).with_name("console.css").read_bytes()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS access (
                    kind TEXT NOT NULL, name TEXT NOT NULL, mode TEXT NOT NULL,
                    PRIMARY KEY (kind, name)
                );
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
                    tool TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL
                );
            """)

    def connect(self):
        return sqlite3.connect(self.db, timeout=3, factory=ClosingConnection)

    def access(self, kind):
        with self.connect() as db:
            return dict(db.execute("SELECT name, mode FROM access WHERE kind = ?", (kind,)))

    def tool_allowed(self, name, arguments):
        try:
            default = "off" if name in OPTIONAL_TOOLS else "on"
            if self.access("tool").get(name, default) == "off":
                return False
            if name == "home_device_control":
                target = arguments.get("entity_id") if isinstance(arguments, dict) else None
                return isinstance(target, str) and self.access("entity").get(target) not in {"read", "hidden"}
            return True
        except sqlite3.Error:
            logging.exception("Could not read Codie tool permissions")
            return False

    def visible_home_status(self, arguments):
        try:
            hidden = {entity for entity, mode in self.access("entity").items() if mode == "hidden"}
        except sqlite3.Error as error:
            raise ValueError("Could not read device permissions") from error
        if not hidden:
            return self.home_status(arguments)
        offset = arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid status offset")
        matches = [row for row in self.home_rows(arguments) if row["id"] not in hidden]
        return {"devices": matches[offset:offset + PAGE_SIZE], "total": len(matches), "offset": offset}

    def home_rows(self, arguments=None):
        arguments = arguments or {}
        for offset in range(0, 2000, PAGE_SIZE):
            page = self.home_status({**arguments, "offset": offset})
            yield from page["devices"]
            if offset + PAGE_SIZE >= page["total"]:
                return
        raise ValueError("Home Assistant inventory is too large for one request")

    def set_access(self, kind, name, mode):
        if kind == "tool":
            valid = name in self.tools and mode in {"on", "off"}
        else:
            valid = kind == "entity" and ENTITY_ID.fullmatch(name) and mode in MODES
        if not valid:
            raise ValueError("Invalid permission")
        with self.connect() as db:
            if mode == "default" or (mode == "on" and name not in OPTIONAL_TOOLS):
                db.execute("DELETE FROM access WHERE kind = ? AND name = ?", (kind, name))
            else:
                db.execute("INSERT OR REPLACE INTO access VALUES (?, ?, ?)", (kind, name, mode))

    def record(self, name, outcome, started):
        if name not in self.tools:
            return
        at = int(time.time())
        duration = round((time.monotonic() - started) * 1000)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO activity (at, tool, outcome, duration_ms) VALUES (?, ?, ?, ?)",
                           (at, name, outcome, duration))
                db.execute("DELETE FROM activity WHERE at < ? OR id <= last_insert_rowid() - 5000",
                           (at - 30 * 86400,))
        except sqlite3.Error:
            logging.exception("Could not record Codie tool activity")

    def connections(self, home_state):
        try:
            discord = Path("/run/codie-discord/state").read_text().strip()
        except FileNotFoundError:
            discord = "off" if Path(DISCORD_CONTROL).is_socket() else "not_configured"
        except OSError:
            discord = "unknown"
        return [
            ("Codie", "Connected" if self.executor_online else "Reconnecting", "Tool executor"),
            ("Home Assistant", home_state, "Device bridge"),
            ("MQTT", "Connected" if self.mqtt_online else "Reconnecting", "Announcements"),
            ("Discord", {"online": "Connected", "off": "Off",
                         "connecting": "Reconnecting", "not_configured": "Not configured"}.get(discord, "Unknown"), "Chat gateway"),
        ]

    def start(self):
        Thread(target=self.serve_web, daemon=True).start()

    def serve_web(self):
        while True:
            try:
                with self.web_server() as server:
                    server.serve_forever()
                return
            except (OSError, ValueError, KeyError) as error:
                logging.warning("Console listener could not start (%s). Retrying.", type(error).__name__)
                time.sleep(10)

    def web_server(self):
        listen = IPv4Address(os.environ["CODIE_CONSOLE_LAN"])
        network = IPv4Network(os.environ["CODIE_CONSOLE_NETWORK"])
        if listen not in network or listen.is_unspecified:
            raise ValueError("The console address must be inside the allowed network")
        host = str(listen)
        server = ConsoleServer((host, PORT), Handler, bind_and_activate=False)
        server.console = self
        server.authority = f"{host}:{PORT}"
        server.origin = f"https://{server.authority}"
        server.network = network
        try:
            server.server_bind()
            credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
            server.password_hash = bytes.fromhex((credentials / "console-password.sha256").read_text().strip())
            if len(server.password_hash) != 32:
                raise ValueError("Invalid console password digest")
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(credentials / "console.crt", credentials / "console.key")
            # Wrap accepted connections in their worker threads to avoid serializing TLS handshakes.
            server.tls = tls
            server.server_activate()
        except Exception:
            server.server_close()
            raise
        return server

    def dashboard(self, saved, query, discord_result=""):
        try:
            devices = list(self.home_rows({"query": query})) if self.home_status else []
            home_state = "Connected" if self.home_status else "Not configured"
        except (OSError, ValueError, KeyError, TypeError):
            devices, home_state = [], "Unavailable"
        cards = "".join(
            f'<li><h3>{escape(label)}</h3><span class="tag {status.lower().replace(" ", "-")}">'
            f'{escape(status)}</span><p>{escape(detail)}</p>'
            + (self.discord_form(status) if label == "Discord" else "") + "</li>"
            for label, status, detail in self.connections(home_state)
        )
        tool_access = self.access("tool")
        tools = "".join(
            f'<div class="row"><div><strong>{escape(LABELS.get(name, name))}</strong>'
            f'<small>{escape(name)}</small></div>'
            + self.form("tool", name, {"on": "Allowed", "off": "Blocked"}, tool_access.get(name, "on"),
                        LABELS.get(name, name))
            + "</div>" for name in self.tools if name not in OPTIONAL_TOOLS
        )
        for name, (caption, detail) in OPTIONAL_TOOLS.items():
            if name not in self.tools:
                continue
            enabled = tool_access.get(name) == "on"
            action = "off" if enabled else "on"
            label = f"Turn {action} {caption}"
            title = LABELS[name]
            tools += (
                f'<div class="row"><div><strong>{title}</strong>'
                f'<small>{"On" if enabled else "Off"}{detail}</small></div>'
                f'<form method="post" action="/access" aria-label="{title}">'
                f'<input type="hidden" name="csrf" value="{self.csrf}">'
                '<input type="hidden" name="kind" value="tool">'
                f'<input type="hidden" name="name" value="{name}">'
                f'<button type="submit" name="mode" value="{action}">{label}</button></form></div>'
            )
        entity_access = self.access("entity")
        device_rows = "".join(
            f'<div class="row"><div><strong>{escape(device["name"])}</strong>'
            f'<small>{escape(device["id"])}</small></div>'
            + self.form("entity", device["id"], MODES, entity_access.get(device["id"], "default"),
                        device["name"])
            + "</div>" for device in devices
        )
        with self.connect() as db:
            recent = db.execute("SELECT at, tool, outcome, duration_ms FROM activity "
                                "WHERE at >= ? ORDER BY id DESC LIMIT 60", (int(time.time()) - 30 * 86400,))
            history = "".join(self.activity_row(*row) for row in recent)
        if not history:
            history = '<tr><td colspan="4">New tool calls will appear here.</td></tr>'
        discord_notice = {
            "on": "Discord service started.",
            "off": "Discord is off.",
            "failed": "Couldn’t change Discord’s connection. Refresh and try again.",
        }.get(discord_result)
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codie Bridge Console</title><link rel="stylesheet" href="/console.css"></head>
<body><header><div class="frame mast"><div class="brand">Codie Bridge</div>
<nav aria-label="Sections"><a href="#connections">Connections</a><a href="#access">Codie access</a>
<a href="#activity">Activity</a></nav></div></header>
<main class="frame"><div class="intro"><div><h1>Bridge console</h1>
<p>Connections, permissions and tool activity.</p></div><a class="button secondary" href="/">Refresh</a></div>
{('<p class="notice" role="status">Permission saved.</p>' if saved else '')}
<section id="connections"><h2>Connections</h2>
{f'<p class="notice" role="status">{discord_notice}</p>' if discord_notice else ''}
<ul class="cards">{cards}</ul></section>
<section id="access"><h2>Codie access</h2><div class="panels">
<article><h3>Bridge tools</h3><p>Blocked tools are refused at the bridge.</p>{tools}</article>
<article id="devices"><h3>Home Assistant devices</h3>
<p>Existing access follows Home Assistant’s rules. Hidden removes the device from status and blocks control.
Front door snapshots have a separate tool setting.</p>
<search><form action="/#devices" method="get" aria-label="Filter Home Assistant devices">
<label for="device-filter">Find a device</label><input id="device-filter" name="q" type="search"
value="{escape(query)}" maxlength="120"><button type="submit">Find</button>
{('<a href="/#devices">Clear</a>' if query else '')}</form></search>
<div class="devices" tabindex="0" role="region" aria-label="Device permissions">
{device_rows or ('<p>No devices match.</p>' if query and home_state == 'Connected' else '<p>No devices loaded.</p>')}
</div></article></div></section>
<section id="activity"><h2>Tool activity</h2><p>Latest 60 calls since the console was enabled. Times use the Pi’s time zone.</p>
<div class="table" tabindex="0" role="region" aria-label="Recent tool activity">
<table><thead><tr><th scope="col">Time</th><th scope="col">Tool</th><th scope="col">Outcome</th>
<th scope="col">Duration</th></tr></thead><tbody>{history}</tbody></table></div></section>
</main></body></html>"""

    def form(self, kind, name, options, current, label):
        options_html = "".join(f'<option value="{key}"{" selected" if key == current else ""}>{value}</option>'
                               for key, value in options.items())
        return (
            f'<form method="post" action="/access" aria-label="{escape(label)} permission">'
            f'<input type="hidden" name="csrf" value="{self.csrf}">'
            f'<input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="name" value="{escape(name)}">'
            f'<select name="mode" aria-label="{escape(label)} access">{options_html}</select>'
            '<button type="submit">Save</button></form>'
        )

    def discord_form(self, status):
        if not Path(DISCORD_CONTROL).is_socket():
            return ""
        action, label = ("on", "Turn on Discord") if status == "Off" else ("off", "Turn off Discord")
        return (
            '<form class="discord-control" method="post" action="/discord" aria-label="Discord connection">'
            f'<input type="hidden" name="csrf" value="{self.csrf}">'
            f'<button type="submit" name="action" value="{action}">{label}</button>'
            '<a href="/discord">Choose channel</a></form>'
        )

    def discord_page(self, change):
        result = discord_request("channels")
        notice = {
            "saved": "Discord channel saved.",
            "unavailable": "The bot can’t use that channel. Choose an available channel below.",
            "failed": "The channel could not be changed.",
        }.get(change)
        if not result.get("ok"):
            settings = "<p>Couldn’t load channels from Discord. Refresh to retry.</p>"
        elif not result["channels"]:
            settings = "<p>The bot needs permission to read and send messages in a text channel.</p>"
        else:
            options = []
            for channel in result["channels"]:
                name = "#" + channel["name"]
                if channel["category"]:
                    name += " · " + channel["category"]
                selected = " selected" if channel["id"] == result["selected"] else ""
                options.append(f'<option value="{escape(channel["id"])}"{selected}>{escape(name)}</option>')
            if not any(channel["id"] == result["selected"] for channel in result["channels"]):
                options.insert(0, '<option value="" selected disabled>Current channel unavailable</option>')
            settings = (
                '<form method="post" action="/discord/channel" class="channel-picker" aria-label="Discord text channel">'
                f'<input type="hidden" name="csrf" value="{self.csrf}">'
                '<label for="discord-channel">Text channel</label>'
                f'<select id="discord-channel" name="channel" required>{"".join(options)}</select>'
                '<button type="submit">Save channel</button></form>'
            )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Discord channel · Codie Bridge</title><link rel="stylesheet" href="/console.css"></head>
<body><header><div class="frame mast"><div class="brand">Codie Bridge</div>
<nav aria-label="Console"><a href="/#connections">Back to connections</a></nav></div></header>
<main class="frame"><div class="intro"><div><h1>Discord channel</h1>
<p>The bot listens in one channel in your Discord server.</p></div></div>
{f'<p class="notice" role="status">{notice}</p>' if notice else ''}
<section class="channel-settings" aria-label="Channel settings"><article>{settings}</article></section>
</main></body></html>"""

    @staticmethod
    def activity_row(at, tool, outcome, duration):
        local = datetime.fromtimestamp(at, timezone.utc).astimezone()
        return (
            f'<tr><td><time datetime="{local.isoformat()}">{local:%b %d %H:%M:%S}</time></td>'
            f'<th scope="row">{escape(LABELS.get(tool, tool))}</th>'
            f'<td><span class="tag {escape(outcome)}">{escape(outcome.title())}</span></td>'
            f'<td>{duration:,} ms</td></tr>'
        )


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class ConsoleServer(ThreadingHTTPServer):
    tls = None

    def verify_request(self, request, client_address):
        return ip_address(client_address[0]) in self.network

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(10)
        try:
            with self.tls.wrap_socket(request, server_side=True) as secured:
                super().finish_request(secured, client_address)
        except (ssl.SSLError, TimeoutError, ConnectionError):
            pass


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_args):
        pass

    def authorized(self):
        if self.headers.get_all("Host") != [self.server.authority]:
            self.reply(403)
            return False
        digest = self.server.password_hash
        scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                username, separator, password = base64.b64decode(encoded, validate=True).partition(b":")
                supplied = hashlib.sha256(password).digest()
                if separator and username == b"codie" and hmac.compare_digest(supplied, digest):
                    return True
            except (ValueError, binascii.Error):
                pass
        self.reply(401, b"Sign in to Codie Bridge.", authenticate=True)
        return False

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", location=None, authenticate=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        if authenticate:
            self.send_header("WWW-Authenticate", 'Basic realm="Codie Bridge", charset="UTF-8"')
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            return
        path = urlsplit(self.path)
        console = self.server.console
        if path.path == "/console.css":
            return self.reply(200, console.css, "text/css; charset=utf-8")
        if path.path == "/":
            fields = parse_qs(path.query)
            query = fields.get("q", [""])[0].strip()[:120]
            page = console.dashboard(path.query == "saved", query, fields.get("discord", [""])[0])
            return self.reply(200, page.encode(), "text/html; charset=utf-8")
        if path.path == "/discord":
            change = parse_qs(path.query).get("channel", [""])[0]
            return self.reply(200, console.discord_page(change).encode(), "text/html; charset=utf-8")
        if path.path.startswith("/image/") and console.image_worker:
            status, picture = console.image_worker.lookup(path.path.removeprefix("/image/"), "chat")
            if picture:
                return self.reply(200, picture, "image/jpeg")
            if status == "queued":
                body = b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>Generating image</title><p>Generating your image. This page refreshes automatically.</p>'
                return self.reply(200, body, "text/html; charset=utf-8")
            return self.reply(404 if status == "missing" else 409, b"This image is unavailable.")
        return self.reply(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reply(400)
        if not 0 < length <= 4096:
            return self.reply(413)
        body = self.rfile.read(length)
        if not self.authorized():
            return
        if self.headers.get("Origin") != self.server.origin:
            return self.reply(403)
        if self.path not in {"/access", "/discord", "/discord/channel"}:
            return self.reply(404)
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.reply(415)
        try:
            fields = parse_qs(body.decode(), strict_parsing=True, max_num_fields=4)
            console = self.server.console
            if not hmac.compare_digest(fields["csrf"][0].encode(), console.csrf.encode()):
                return self.reply(403)
            if self.path == "/discord":
                return self.post_discord(fields["action"][0])
            if self.path == "/discord/channel":
                channel = fields["channel"][0]
                if not DISCORD_CHANNEL_ID.fullmatch(channel):
                    return self.reply(400, b"Invalid Discord channel.")
                result = discord_request(f"channel {channel}")
                state = "saved" if result.get("ok") else (
                    "unavailable" if result.get("error") == "unavailable" else "failed"
                )
                return self.reply(303, location=f"/discord?channel={state}")
            kind, name, mode = (fields[key][0] for key in ("kind", "name", "mode"))
            console.set_access(kind, name, mode)
        except (ValueError, KeyError):
            return self.reply(400, b"Invalid permission.")
        except sqlite3.Error:
            return self.reply(503, b"The permission could not be saved. Try again.")
        return self.reply(303, location="/?saved#" + ("devices" if kind == "entity" else "access"))

    def post_discord(self, action):
        if action not in {"on", "off"}:
            return self.reply(400, b"Invalid Discord action.")
        if not discord_request(action).get("ok"):
            action = "failed"
        return self.reply(303, location=f"/?discord={action}#connections")
