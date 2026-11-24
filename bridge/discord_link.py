"""Local Codie replies and optional image jobs for the Discord service."""
import base64
import json
import logging
from pathlib import Path
import socket
from uuid import uuid4

from discord_intent import PROMPT, parse_reply
from openai_images import ImageError

PATH = Path("/var/lib/codie-discord-link/device.sock")


def reply(api, wait_for_task, request, images=None):
    operation = request.get("operation")
    if operation == "image_status":
        if images is None:
            return {"ok": True, "status": "failed"}
        status, picture = images.lookup(request.get("id"), "discord")
        result = {"ok": True, "status": status}
        if picture:
            result["image"] = base64.b64encode(picture).decode("ascii")
        return result
    if operation == "status":
        api.request("GET", "/data/user/slot")
        return {"ok": True, "answer": "The Discord bot, Pi bridge, and Codie are connected."}
    question = request.get("question")
    if operation not in {"ask", "respond"} or not isinstance(question, str) or not 2 <= len(question.strip()) <= 400:
        raise ValueError("Invalid Discord request")
    history = request.get("history", [])
    if not isinstance(history, list) or len(history) > 2:
        raise ValueError("Invalid Discord conversation")
    for turn in history:
        if (
            not isinstance(turn, list) or len(turn) != 2
            or not isinstance(turn[0], str) or len(turn[0]) > 200
            or not isinstance(turn[1], str) or len(turn[1]) > 300
        ):
            raise ValueError("Invalid Discord conversation turn")
    context = [{"user": user, "maya": maya} for user, maya in history]
    conversation = json.dumps({"earlier_discord_replies": context, "current_user_message": question.strip()},
                              ensure_ascii=False)
    use_images = operation == "respond" and images is not None
    instructions = PROMPT if use_images else (
        "Reply briefly to this Discord message. Keep private details and unrelated conversations out of the reply."
    )
    slot = api.request("GET", "/data/user/slot")["slot_number"]
    turn = api.request("POST", "/data/user/chat/turns", {
        "request_id": str(uuid4()), "slot": slot,
        "input": {"type": "text", "text": instructions + "\nDiscord input: " + conversation},
        "output": {"speech": "none"}, "interrupt": False, "retention": "none", "tools": "none",
    })
    result = wait_for_task(api, f"/data/user/chat/turns/{turn['turn_id']}", 60)
    answer = result.get("assistant_text")
    if result["state"] != "completed" or not isinstance(answer, str) or not answer.strip():
        raise ValueError("Codie did not answer")
    if not use_images:
        return {"ok": True, "kind": "chat", "answer": answer[:1500]}
    plan = parse_reply(answer)
    if plan["kind"] == "chat":
        return {"ok": True, **plan}
    try:
        result = images.enqueue(request["id"], plan["prompt"], "discord", include_maya=plan["include_maya"])
    except ImageError as error:
        return {"ok": True, "kind": "image", "status": "failed", "detail": str(error)}
    return {"ok": True, "kind": "image", "prompt": plan["prompt"], **result}


def serve(api, wait_for_task, images=None):
    PATH.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(PATH))
        PATH.chmod(0o660)
        server.listen(2)
        while True:
            client, _ = server.accept()
            with client:
                try:
                    client.settimeout(4)
                    with client.makefile("rb") as stream:
                        raw = stream.readline(8192)
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("Expected a request object")
                    response = reply(api, wait_for_task, request, images)
                except Exception as exc:
                    logging.warning("Discord bridge request failed (%s)", type(exc).__name__)
                    response = {"ok": False}
                try:
                    client.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass
