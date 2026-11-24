#!/usr/bin/env python3
"""LAN bridge between Codie's external tool API and a local MQTT broker."""

from getpass import getpass
import hashlib
import hmac
import http.client
import json
import logging
import os
from pathlib import Path
from queue import Full, Queue
import socket
import sys
from threading import Thread
import time
from urllib.parse import unquote, urlencode, urlsplit
from uuid import UUID, uuid4

import paho.mqtt.client as mqtt
import websocket

import front_camera
import home_devices

CODIE_HOST = os.environ.get("CODIE_HOST", "")
CODIE_PORT = 8081
INTEGRATION_ID = os.environ.get("CODIE_INTEGRATION_ID", "")
ENABLE_HOME = os.environ.get("CODIE_ENABLE_HOME") == "1"
ENABLE_CAMERA = ENABLE_HOME and os.environ.get("CODIE_ENABLE_CAMERA") == "1"
TOPIC = "codie/bridge/announce"
ROOT = "/v1/api"
CATALOG_PATH = f"/data/user/tool-integrations/{INTEGRATION_ID}"
TOOLS = [{
    "name": "bridge_status",
    "version": "v1",
    "description": "Check whether the Codie Pi Bridge Spellbook is online. Call this to obtain its hostname and a fresh proof code, and report that code exactly.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    "result_schema": {
        "type": "object",
        "properties": {"hostname": {"type": "string"}, "proof": {"type": "string"}},
        "required": ["hostname", "proof"],
        "additionalProperties": False,
    },
    "risk": "read",
    "idempotent": True,
    "confirmation_required": False,
    "timeout_ms": 5000,
}]


class APIError(Exception):
    def __init__(self, status, code):
        super().__init__(f"Codie HTTP {status} ({code})")
        self.status = status


class Codie:
    def __init__(self, credential=None):
        self.credential = credential

    def sign(self, method, path, mode, body):
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        body_hash = "UNSIGNED-PAYLOAD" if mode == "multipart" else hashlib.sha256(body).hexdigest()
        user_id = self.credential["user_id"]
        canonical = "|".join((method, unquote(urlsplit(path).path), timestamp,
                              nonce, mode, body_hash, user_id))
        parameters = {
            "sig": self.mac(canonical), "timestamp": timestamp, "nonce": nonce,
            "payload_mode": mode, "body_hash": body_hash, "user_id": user_id,
        }
        if self.credential.get("id"):
            parameters["session_id"] = self.credential["id"]
        return parameters

    def mac(self, value):
        return hmac.new(self.credential["secret"].encode(), value.encode(), "sha256").hexdigest()

    def request(self, method, route, payload=None, *, multipart=None):
        path = ROOT + route
        if multipart is None:
            body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
            content_type, mode = "application/json", "json"
        else:
            body, content_type = multipart
            mode = "multipart"
        headers = {"Content-Type": content_type}
        if self.credential:
            # JSON with the empty-body hash is also used by CODE27's agent daemon.
            signed = self.sign(method, path, mode, body)
            headers.update({"X-Local-" + field.replace("_", "-"): value for field, value in signed.items()})
        connection = http.client.HTTPConnection(CODIE_HOST, CODIE_PORT, timeout=8)
        try:
            connection.request(method, path, body if body else None, headers)
            response = connection.getresponse()
            data = json.loads(response.read(2**20)) if response.status != 204 else {}
            if not 200 <= response.status < 300 or data.get("success") is False:
                code = data.get("code")
                raise APIError(response.status, code if isinstance(code, str) else "request failed")
            return data.get("data")
        finally:
            connection.close()

    def executor(self):
        path = f"/v1/ws/tool-integrations/{INTEGRATION_ID}/executor"
        challenge = uuid4().hex
        query = self.sign("GET", path, "empty", b"")
        query["challenge"] = challenge
        connection = websocket.create_connection(
            f"ws://{CODIE_HOST}:{CODIE_PORT}{path}?{urlencode(query)}", timeout=45,
        )
        try:
            ready = json.loads(connection.recv())
            proof = ready.get("data", {}).get("proof", "")
            expected = self.mac(f"codie-device-proof|v1|ws:{INTEGRATION_ID}|{challenge}")
            if ready.get("type") != "ready" or not hmac.compare_digest(proof, expected):
                raise RuntimeError("Codie did not confirm the executor connection")
            yield connection
        finally:
            connection.close()


def pair(challenge, destination, code):
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError("Expected a six-digit pairing code")
    unsigned = Codie()
    user_id = unsigned.request("GET", "/auth/bootstrap")["user_id"]
    paired = unsigned.request("POST", "/auth/pair/confirm", {
        "challenge_id": challenge, "pairing_code": code, "auth_scope": "permanent",
    })
    master = Codie({"secret": paired["local_secret"], "user_id": user_id})
    credential = master.request("POST", "/auth/credentials", {"label": "Codie Pi Bridge Spellbook"})
    record = {"secret": credential["secret"], "id": credential["id"], "user_id": user_id}
    Codie(record).request("GET", "/data/user/system")
    os.umask(0o077)
    with open(destination, "x", encoding="utf-8") as handle:
        json.dump(record, handle)
    print("Dedicated Codie credential verified and saved.")


def read_credential():
    location = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/codie-spellbook"))
    return json.loads((location / "codie.json").read_text(encoding="utf-8"))


def register_tool(api, catalog):
    revision = 0
    try:
        revision = api.request("GET", CATALOG_PATH)["revision"]
    except APIError as error:
        if error.status != 404:
            raise
    api.request("PUT", CATALOG_PATH, {
        "protocol_version": "v1", "expected_revision": revision,
        "name": "Codie Pi Bridge Spellbook", "enabled": True, "tools": catalog,
    })


def tool_loop(api, camera_pending):
    catalog = list(TOOLS)
    if ENABLE_HOME:
        catalog.extend((home_devices.STATUS, home_devices.CONTROL))
    if ENABLE_CAMERA:
        catalog.append(front_camera.TOOL)
    registered = False
    delay = 1
    while True:
        try:
            if not registered:
                register_tool(api, catalog)
                registered = True
            for conn in api.executor():
                logging.info("Codie tool executor online")
                delay = 1
                while raw := conn.recv():
                    message = json.loads(raw)
                    if message.get("type") != "invoke":
                        continue
                    call = message["data"]
                    status = "succeeded"
                    name = call.get("name")
                    response = {"invocation_id": call["invocation_id"]}
                    if name == "bridge_status":
                        proof = f"BRIDGE-{uuid4().hex[:8].upper()}"
                        result = {"hostname": socket.gethostname(), "proof": proof}
                        logging.info("Codie invoked bridge_status, proof=%s", proof)
                    elif ENABLE_CAMERA and name == front_camera.TOOL["name"]:
                        try:
                            result = front_camera.submit(api, camera_pending, call.get("arguments") or {})
                        except (OSError, ValueError, TypeError, KeyError, APIError):
                            status = "failed"
                            response["code"] = "FRONT_DOOR_CAMERA_NOT_QUEUED"
                            result = {"status": "unavailable", "detail": "The camera request could not be queued."}
                    elif ENABLE_HOME and name in {"home_device_status", "home_device_control"}:
                        arguments = call.get("arguments") or {}
                        try:
                            result = home_devices.invoke(name, arguments)
                            logging.info("%s succeeded (target=%s, action=%s, reported_state=%s)",
                                         name, arguments.get("entity_id", arguments.get("domain", "")),
                                         arguments.get("action", ""), result.get("device", {}).get("state", ""))
                        except (OSError, ValueError, TypeError, KeyError) as error:
                            logging.warning("%s failed (%s)", name, type(error).__name__)
                            status = "failed"
                            response["code"] = "HOME_DEVICE_REQUEST_FAILED"
                            result = {"detail": str(error)[:240] if isinstance(error, ValueError)
                                      else "Home Assistant device access is unavailable."}
                    else:
                        status = "failed"
                        response["code"] = "UNKNOWN_TOOL"
                        result = {}
                    response.update(status=status, data=result)
                    conn.send(json.dumps({"type": "result", "data": response}))
        except (OSError, ValueError, APIError, RuntimeError, websocket.WebSocketException) as error:
            # WebSocket exceptions can carry the signed URL. Log the kind only.
            logging.warning("Codie tool link disconnected (%s)", type(error).__name__)
            if isinstance(error, websocket.WebSocketBadStatusException) and error.status_code == 404:
                registered = False
        time.sleep(delay)
        delay = min(delay * 2, 30)


def wait_for_task(api, route, limit):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        task = api.request("GET", route)
        if task["state"] in {"completed", "failed", "cancelled"}:
            return task
        time.sleep(1)
    raise TimeoutError("Codie task did not reach a terminal state")


def announce(api, event_id, text):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        display = api.request("GET", "/data/user/device/state")
        backend = api.request("GET", "/data/user/ai/backend")
        if not display.get("locked") and not display.get("conversation_blocked") and backend.get("active_state") == "Idle":
            slot = api.request("GET", "/data/user/slot")["slot_number"]
            task = api.request("POST", "/data/user/tts/utterances", {
                "request_id": event_id, "slot": slot, "text": text,
            })
            result = wait_for_task(api, f"/data/user/tts/utterances/{task['utterance_id']}", 60)
            logging.info("Announcement %s playback %s", event_id, result["state"])
            return
        time.sleep(2)
    logging.warning("Announcement %s expired waiting for Codie to become idle", event_id)


def announcement_loop(api, pending):
    while True:
        received, event_id, text = pending.get()
        if time.monotonic() - received > 60:
            logging.warning("Announcement %s expired in queue", event_id)
            continue
        try:
            announce(api, event_id, text)
        except (OSError, ValueError, APIError) as error:
            logging.warning("Announcement %s failed: %s", event_id, error)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        logging.warning("MQTT connection failed: %s", reason_code)
        return
    client.subscribe(TOPIC, qos=1)
    logging.info("Subscribed to MQTT topic %s", TOPIC)


def on_message(client, pending, message):
    if message.retain or len(message.payload) > 2048:
        return
    try:
        payload = json.loads(message.payload)
        event_id = str(UUID(payload["id"]))
        text = payload["text"].strip()
        if not 0 < len(text) <= 400:
            raise ValueError("Invalid announcement length")
        pending.put_nowait((time.monotonic(), event_id, text))
    except (ValueError, TypeError, KeyError, AttributeError, Full):
        logging.warning("Dropped malformed announcement or full queue")


def serve(api):
    pending = Queue(maxsize=10)
    camera_pending = Queue(maxsize=1)
    if ENABLE_CAMERA:
        Thread(target=front_camera.run, args=(api, camera_pending, wait_for_task, announce, APIError), daemon=True).start()
    Thread(target=tool_loop, args=(api, camera_pending), daemon=True).start()
    Thread(target=announcement_loop, args=(api, pending), daemon=True).start()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=pending)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async("127.0.0.1", 1883)
    client.loop_forever(retry_first_connection=True)


def test_tool(api, message):
    slot = api.request("GET", "/data/user/slot")["slot_number"]
    turn = api.request("POST", "/data/user/chat/turns", {
        "request_id": str(uuid4()), "slot": slot,
        "input": {"type": "text", "text": message},
        "output": {"speech": "none"}, "interrupt": False, "retention": "none", "tools": "all",
    })
    result = wait_for_task(api, f"/data/user/chat/turns/{turn['turn_id']}", 80)
    print(json.dumps({key: result.get(key) for key in ("state", "assistant_text", "error")}))
    return 0 if result["state"] == "completed" else 1


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pair_args = commands.add_parser("pair", help="Create a dedicated local credential")
    pair_args.add_argument("destination", type=Path)
    ask = commands.add_parser("ask", help="Test a Codie conversation turn using the registered tools")
    ask.add_argument("message")
    commands.add_parser("serve", help="Run the tool executor and MQTT subscriber")
    args = parser.parse_args()

    if not CODIE_HOST:
        parser.error("Set CODIE_HOST to the Codie's LAN address.")
    if args.command == "pair":
        if args.destination.exists():
            parser.error("The credential destination already exists.")
        challenge = Codie().request("POST", "/auth/pair/request", {})
        code = getpass("Enter the six-digit pairing code displayed on Codie: ").strip()
        pair(challenge["challenge_id"], args.destination, code)
        return 0
    if args.command == "serve":
        try:
            UUID(INTEGRATION_ID)
        except ValueError:
            parser.error("Set CODIE_INTEGRATION_ID to your generated UUID.")
    api = Codie(read_credential())
    if args.command == "ask":
        return test_tool(api, args.message)
    serve(api)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
