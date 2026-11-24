"""Local, text-only Codie interface for the Discord service."""
import json
import logging
from pathlib import Path
import socket
from uuid import uuid4

PATH = Path("/var/lib/codie-discord-link/device.sock")


def reply(api, wait_for_task, request):
    operation = request.get("operation")
    if operation == "status":
        api.request("GET", "/data/user/slot")
        return {"ok": True, "answer": "The Discord bot, Pi bridge, and Codie are connected."}
    question = request.get("question")
    if operation != "ask" or not isinstance(question, str) or not 2 <= len(question.strip()) <= 400:
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
    prompt = "Current prompt: " + question.strip()
    if history:
        context = [{"user": user, "maya": maya} for user, maya in history]
        prompt = (
            "Use the supplied Discord replies as context for the current prompt. "
            "Answer the current prompt without restating the conversation.\n"
            "Discord replies: " + json.dumps(context, ensure_ascii=False) + "\n" + prompt
        )
    slot = api.request("GET", "/data/user/slot")["slot_number"]
    turn = api.request("POST", "/data/user/chat/turns", {
        "request_id": str(uuid4()), "slot": slot,
        "input": {"type": "text", "text": (
            "Your reply will be posted in a Discord channel. Reply briefly to the current prompt. "
            "Keep personal details and any conversation outside this Discord exchange out of your reply. "
            + prompt
        )},
        "output": {"speech": "none"}, "interrupt": False, "retention": "none", "tools": "none",
    })
    result = wait_for_task(api, f"/data/user/chat/turns/{turn['turn_id']}", 60)
    answer = result.get("assistant_text")
    if result["state"] != "completed" or not isinstance(answer, str) or not answer.strip():
        raise ValueError("Codie did not answer")
    return {"ok": True, "answer": answer[:1500]}


def serve(api, wait_for_task):
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
                    response = reply(api, wait_for_task, request)
                except Exception as exc:
                    logging.warning("Discord bridge request failed (%s)", type(exc).__name__)
                    response = {"ok": False}
                try:
                    client.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass
