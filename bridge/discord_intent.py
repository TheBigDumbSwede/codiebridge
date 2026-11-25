"""Interpret Maya's structured response to an authorized Discord message."""

import json

PROMPT = """
You are replying to an authorized user in Discord. Return exactly one JSON object, with no markdown.
For ordinary conversation return:
{"kind":"chat","answer":"your brief reply to the user"}
Use earlier Discord replies only to understand the current request. Keep private or unrelated
conversation out of replies and requests.
"""

IMAGE_PROMPT = """
If the current user asks you to make a new image, including a polite request or a revision
of a previous generated image, return:
{"kind":"image","prompt":"a self-contained image brief","maya":true}
The Pi has an image generator and will act on that object, even though this chat turn has
no direct tools. Describe the requested scene, style and subject. Use maya=true
only if the user wants you (Maya) in the picture, or wants to keep Maya in a revised scene.
Otherwise set it to false. Resolve references such as "of you" to Maya.
Questions about whether you can make images, requests for descriptions, and quoted examples are ordinary conversation.
Treat a polite question asking for a specific image as a request to make that image.
A general question about capabilities with no image brief is ordinary conversation.
For an image capability question, explain that you can create images in Discord when asked.
"""

OPINION_PROMPT = """
If the current user asks for a second opinion or explicitly asks another model to critique
your reasoning, return:
{"kind":"opinion","question":"a focused standalone question","facts":"only relevant reported facts","draft":"","localview":"your own brief independent assessment"}
Leave draft empty unless the user explicitly asks the upstream model to critique your reasoning.
The upstream model will never be sent localview.
Never put credentials, unrelated conversation, or raw private records into the opinion request.
"""


def parse_reply(answer):
    raw = answer.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.partition("\n")[2][:-3].strip()
    result, end = json.JSONDecoder().raw_decode(raw)
    if raw[end:].strip() not in {"", "."}:
        raise ValueError("Unexpected text following Discord reply")
    if isinstance(result, dict) and result.get("kind") == "chat" and set(result) == {"kind", "answer"}:
        if isinstance(result["answer"], str) and result["answer"].strip():
            return {"kind": "chat", "answer": result["answer"].strip()[:1500]}
    if isinstance(result, dict) and result.get("kind") == "image" and set(result) == {"kind", "prompt", "maya"}:
        if (isinstance(result["prompt"], str) and 3 <= len(result["prompt"].strip()) <= 800
                and type(result["maya"]) is bool):
            return {"kind": "image", "prompt": result["prompt"].strip(), "include_maya": result["maya"]}
    opinion_fields = {"question", "facts", "draft", "localview"}
    if isinstance(result, dict) and result.get("kind") == "opinion" and set(result) == opinion_fields | {"kind"}:
        if all(isinstance(result[field], str) for field in opinion_fields):
            return result
    raise ValueError("Invalid Discord reply format")
