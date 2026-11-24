"""Codie tool catalog and local Home Assistant socket transport."""
import json
import socket

_SOCKET = "/var/lib/codie-spellbook/shared/device.sock"
_OBJECT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string"}, "name": {"type": "string"}, "state": {"type": "string"},
        "device": {"type": "string"},
        "details": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "brightness": {"type": "integer"}, "battery_level": {"type": "integer"},
                "volume_level": {"type": "number"}, "is_volume_muted": {"type": "boolean"},
                "unit_of_measurement": {"type": "string"}, "event_type": {"type": "string"},
                "current_temperature": {"type": "number"}, "temperature": {"type": "number"},
                "min_temp": {"type": "number"}, "max_temp": {"type": "number"},
                "fan_mode": {"type": "string"}, "percentage": {"type": "integer"},
            },
        },
    },
    "required": ["id", "name", "state", "details"],
}

STATUS = {
    "name": "home_device_status", "version": "v2",
    "description": (
        "Read actual Home Assistant device states. Filter by common name or domain such as light, "
        "switch, vacuum, media_player, camera, sensor or event. Use exact device IDs returned here "
        "for control. The total and offset let you page ten results at a time. Cameras provide "
        "status only. Read operations do not change devices."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {"query": {"type": "string"}, "domain": {"type": "string"},
                       "offset": {"type": "integer", "minimum": 0}},
    },
    "result_schema": {
        "type": "object",
        "properties": {"devices": {"type": "array", "items": _OBJECT},
                       "total": {"type": "integer"}, "offset": {"type": "integer"}},
        "required": ["devices", "total", "offset"], "additionalProperties": False,
    },
    "risk": "read", "idempotent": True, "confirmation_required": False, "timeout_ms": 6000,
}

CONTROL = {
    "name": "home_device_control", "version": "v2",
    "description": (
        "Control one requested Hue light, plug, Roomba, TV, speaker, or BedJet. Use an exact entity ID from "
        "home_device_status. For BedJet use its climate entity: cool, heat, turn_off, fan_speed (percent), "
        "or temperature (in the reported unit). Ask if the device is ambiguous. Check state to confirm changes."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False, "required": ["entity_id", "action"],
        "properties": {
            "entity_id": {"type": "string"},
            "action": {"type": "string", "enum": [
                "turn_on", "turn_off", "brightness", "start", "pause", "stop", "dock",
                "volume", "play", "mute", "unmute", "cool", "heat", "fan_speed", "temperature"]},
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "temperature": {"type": "number"},
        },
    },
    "result_schema": {
        "type": "object",
        "properties": {"action": {"type": "string"}, "accepted": {"type": "boolean"}, "device": _OBJECT},
        "required": ["action", "accepted", "device"], "additionalProperties": False,
    },
    "risk": "reversible", "idempotent": False, "confirmation_required": False, "timeout_ms": 14000,
}


def invoke(name, arguments):
    operation = "status" if name == STATUS["name"] else "control"
    request = {**arguments, "operation": operation}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(12 if operation == "control" else 4)
        client.connect(_SOCKET)
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline(32768))
    if "error" in response:
        raise ValueError(response["error"])
    result = response["result"]
    for device in result.get("devices", [result.get("device")]):
        if device is not None:
            device["details"] = {key: value for key, value in device["details"].items() if value is not None}
    return result
