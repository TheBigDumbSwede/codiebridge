"""Generate one image from an explicit request and display it on Codie."""

import base64
from datetime import datetime
import http.client
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from queue import Queue
import sqlite3
from threading import Lock
import time
from uuid import NAMESPACE_URL, uuid4, uuid5
from urllib.parse import quote

from PIL import Image

from front_camera import jpeg, show_picture, wait_idle

LOG = logging.getLogger(__name__)
MODEL = "gpt-image-2.6-flare"
DAILY_LIMIT = 5
MAX_RESPONSE = 8 * 1024 * 1024
MAYA_REFERENCES = ("maya-face.jpg", "maya-outfit.jpg", "maya-full.jpg", "maya-angle.jpg", "maya-front.jpg")
TOOL = {
    "name": "generate_image",
    "version": "v2",
    "description": (
        "Generate one original image using OpenAI and deliver it where the request originated. "
        "Call only when the user explicitly asks to generate, draw, or make a picture. "
        "Send only the image brief. Set include_maya=true when the user asks to include Maya or yourself. "
        "This queues the job, so tell the user it is being generated "
        "and repeat the provided link exactly when one is returned."
    ),
    "parameters": {"type": "object", "additionalProperties": False, "required": ["prompt"],
                   "properties": {"prompt": {"type": "string", "minLength": 3, "maxLength": 800},
                                  "include_maya": {"type": "boolean"}}},
    "result_schema": {"type": "object", "additionalProperties": False, "required": ["status", "detail"],
                      "properties": {"status": {"type": "string"}, "detail": {"type": "string"},
                                     "url": {"type": "string"}}},
    "risk": "reversible", "idempotent": True, "confirmation_required": False, "timeout_ms": 12000,
}


def read_key():
    directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/codie-spellbook"))
    try:
        return (directory / "openai-api-key").read_text().strip()
    except FileNotFoundError:
        return ""


class ImageError(ValueError):
    pass


def image_request(prompt, references):
    fields = {"model": MODEL, "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "low", "output_format": "jpeg"}
    if not references:
        return "/v1/images/generations", json.dumps(fields).encode(), "application/json"
    fields["prompt"] = (
        "Create this scene with Maya, the adult stylized avatar shown in the reference images. "
        "Use the references for Maya’s face, brown bob, gold eyes, and futuristic outfit. "
        "Ignore the reference backgrounds, all lettering, logos and the words Mister Pink. "
        "Do not reproduce those in the result. Scene: " + prompt
    )
    boundary = uuid4().hex
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    for index, picture in enumerate(references):
        chunks += [
            f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="maya-{index}.jpg"\r\n'
            f'Content-Type: image/jpeg\r\n\r\n'.encode(), picture, b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return "/v1/images/edits", b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def generate(key, prompt, references=()):
    path, body, content_type = image_request(prompt, references)
    connection = http.client.HTTPSConnection("api.openai.com", timeout=150)
    try:
        connection.request("POST", path, body, {
            "Authorization": f"Bearer {key}", "Content-Type": content_type,
        })
        response = connection.getresponse()
        if response.status != 200:
            raise ImageError(f"OpenAI HTTP {response.status}")
        raw = response.read(MAX_RESPONSE + 1)
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE:
        raise ImageError("OpenAI image response exceeded the size limit")
    picture = base64.b64decode(json.loads(raw)["data"][0]["b64_json"], validate=True)
    with Image.open(BytesIO(picture)) as image:
        if image.width * image.height > 4_000_000:
            raise ImageError("OpenAI image exceeded the size limit")
        return jpeg(image)


class ImageWorker:
    def __init__(self, api, console, key, api_error):
        self.api = api
        self.console = console
        self.key = key
        self.api_error = api_error
        self.pending = Queue(maxsize=1)
        self.jobs = {}
        self.lock = Lock()
        self.origin = f'https://{os.environ["CODIE_CONSOLE_LAN"]}:8788'
        directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/codie-spellbook"))
        self.references = tuple((directory / name).read_bytes() for name in MAYA_REFERENCES
                                if (directory / name).is_file())
        with console.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS image_requests "
                       "(id TEXT PRIMARY KEY, accepted_at INTEGER NOT NULL)")

    def native_request(self, call, arguments):
        slot = self.api.request("GET", "/data/user/slot")["slot_number"]
        lane = "chat"
        for turn_id in dict.fromkeys((call.get("turn_id"), call.get("session_id"))):
            if not turn_id:
                continue
            try:
                turn = self.api.request("GET", "/data/user/chat/turns/" + quote(str(turn_id), safe=""))
            except self.api_error as error:
                if error.status not in {403, 404}:
                    raise
                continue
            if (turn.get("input_type") in {"device_microphone", "audio_file", "audio_stream"}
                    and turn.get("output", {}).get("speech") == "device"):
                lane = "screen"
            break
        return self.enqueue(call["invocation_id"], arguments.get("prompt"), lane, slot,
                            include_maya=arguments.get("include_maya") is True)

    def enqueue(self, invocation, prompt, lane, slot=None, include_maya=False):
        if not self.console.tool_allowed(TOOL["name"], {}):
            raise ImageError("OpenAI image generation is turned off in the Pi console.")
        if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 800:
            raise ImageError("Supply an image description of 3 to 800 characters.")
        if include_maya and not self.references:
            raise ImageError("Maya’s reference images are not configured.")
        job = str(uuid5(NAMESPACE_URL, f"codie:image:{lane}:{invocation}"))
        result = {"status": "queued", "detail": "OpenAI will generate the picture in the background."}
        if lane == "chat":
            result["url"] = f"{self.origin}/image/{job}"
            result["detail"] = "Open the image link to view it when ready."
        if lane == "discord":
            result["job"] = job
        with self.lock:
            if job in self.jobs:
                return result
            if self.pending.full():
                raise ImageError("There is already an image waiting to be generated.")
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            now = int(time.time())
            with self.console.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM image_requests WHERE accepted_at < ?", (now - 7 * 86400,))
                if db.execute("SELECT 1 FROM image_requests WHERE id = ?", (job,)).fetchone():
                    raise ImageError("This image request was already accepted.")
                if db.execute("SELECT count(*) FROM image_requests WHERE accepted_at >= ?", (midnight,)).fetchone()[0] >= DAILY_LIMIT:
                    raise ImageError("The Pi has reached its five-image daily limit.")
                db.execute("INSERT INTO image_requests VALUES (?, ?)", (job, now))
            self.jobs = {key: row for key, row in self.jobs.items() if row[0] > now - 3600}
            self.jobs[job] = (now, lane, "queued", None)
            self.pending.put_nowait((job, lane, slot, prompt.strip(), include_maya))
        return result

    def lookup(self, job, lane):
        with self.lock:
            row = self.jobs.get(job)
            if not row or row[1] != lane or row[0] <= time.time() - 3600:
                return "missing", None
            return row[2:]

    def run(self):
        while True:
            job, lane, slot, prompt, include_maya = self.pending.get()
            started = time.monotonic()
            outcome = "failed"
            image = None
            try:
                if not self.console.tool_allowed(TOOL["name"], {}):
                    outcome = "blocked"
                else:
                    references = self.references if include_maya else ()
                    image = generate(self.key, prompt, references)
                    LOG.info("OpenAI generated image %s for %s with %d avatar references", job, lane, len(references))
                    outcome = "generated"
                    if lane == "screen":
                        self.console.record(TOOL["name"], outcome, started)
                        wait_idle(self.api, slot, time.monotonic() + 40)
                        if self.console.tool_allowed(TOOL["name"], {}):
                            if self.api.request("GET", "/data/user/device/state").get("screen_on") is False:
                                self.api.request("PUT", "/data/user/device/screen", {"on": True})
                            if show_picture(self.api, job, image):
                                outcome = "displayed"
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException, self.api_error) as error:
                LOG.warning("OpenAI image job %s: %s", job, str(error) if isinstance(error, ImageError) else type(error).__name__)
            if outcome != "generated" or lane != "screen":
                self.console.record(TOOL["name"], outcome, started)
            with self.lock:
                accepted_at = self.jobs[job][0]
                self.jobs[job] = (accepted_at, lane, outcome, image if lane != "screen" else None)
