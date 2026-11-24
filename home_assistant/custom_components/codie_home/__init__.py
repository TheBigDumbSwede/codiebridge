"""Local, restricted Home Assistant device access for the Codie bridge."""
import asyncio
import json
import logging
import os
from pathlib import Path

import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry, entity_registry

_LOGGER = logging.getLogger(__name__)
_SOCKET = Path("/run/codie-home/device.sock")
DOMAIN = "codie_home"
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional("read_entities", default=[]): [cv.entity_id],
        vol.Optional("write_entities", default=[]): [cv.entity_id],
        vol.Optional("front_door_camera"): cv.entity_id,
    }),
}, extra=vol.ALLOW_EXTRA)
_PLATFORMS = {
    "matter": {"light", "switch", "event", "binary_sensor", "sensor", "number", "select", "button"},
    "nest": {"camera", "event", "sensor", "binary_sensor"},
    "bedjet": {"climate", "fan", "sensor", "binary_sensor", "switch"},
    "cast": {"media_player"}, "androidtv_remote": {"media_player", "remote"},
    "roomba": {"vacuum", "sensor", "binary_sensor"}, "ipp": {"sensor"},
}
_CONTROLS = {
    "light": {"turn_on": "turn_on", "turn_off": "turn_off", "brightness": "turn_on"},
    "switch": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "climate": {
        "turn_off": "set_hvac_mode", "cool": "set_hvac_mode", "heat": "set_hvac_mode",
        "fan_speed": "set_fan_mode", "temperature": "set_temperature",
    },
    "vacuum": {"start": "start", "pause": "pause", "stop": "stop", "dock": "return_to_base"},
    "media_player": {
        "turn_on": "turn_on", "turn_off": "turn_off", "volume": "volume_set",
        "play": "media_play", "pause": "media_pause", "stop": "media_stop",
        "mute": "volume_mute", "unmute": "volume_mute",
    },
}
_FIELDS = {
    "light": ("brightness",), "vacuum": ("battery_level",),
    "media_player": ("volume_level", "is_volume_muted"),
    "climate": ("current_temperature", "temperature", "fan_mode", "min_temp", "max_temp"),
    "fan": ("percentage",),
    "sensor": ("unit_of_measurement",), "event": ("event_type",),
}


def visible(entry):
    return (
        entry is not None and entry.disabled_by is None
        and entry.device_id is not None
        and entry.domain in _PLATFORMS.get(entry.platform, ())
    )


def describe(hass, entry):
    state = hass.states.get(entry.entity_id)
    info = {"id": entry.entity_id, "name": entry.name or entry.original_name or entry.entity_id,
            "state": "unavailable", "details": {}}
    if state is not None:
        info["name"] = state.name
        info["state"] = state.state[:160]
        info["details"] = {k: state.attributes[k] for k in _FIELDS.get(entry.domain, ()) if k in state.attributes}
    if entry.domain == "climate":
        info["details"]["unit_of_measurement"] = hass.config.units.temperature_unit
    device = device_registry.async_get(hass).async_get(entry.device_id)
    if device:
        info["device"] = device.name_by_user or device.name or ""
    return info


def search(hass, registry, allowed, request):
    query = request.get("query", "")
    domain = request.get("domain", "")
    offset = request.get("offset", 0)
    if not isinstance(query, str) or not isinstance(domain, str) or type(offset) is not int or offset < 0:
        raise ValueError("Invalid status filter")
    words = query.casefold().replace("_", " ").split()
    matches = []
    for entry in sorted(registry.entities.values(), key=lambda item: item.entity_id):
        if entry.entity_id not in allowed or not visible(entry) or (domain and entry.domain != domain):
            continue
        row = describe(hass, entry)
        haystack = " ".join((row["id"], row["name"], row.get("device", ""))).casefold().replace("_", " ")
        if all(word in haystack for word in words):
            matches.append(row)
    return {"devices": matches[offset:offset + 10], "total": len(matches), "offset": offset}


async def control(hass, registry, allowed, request):
    entity_id = request.get("entity_id")
    action = request.get("action")
    if not isinstance(entity_id, str) or not isinstance(action, str):
        raise ValueError("Provide one exact entity ID and an action")
    entry = registry.async_get(entity_id)
    if entity_id not in allowed or not visible(entry) or action not in _CONTROLS.get(entry.domain, {}):
        raise ValueError("This device or action is outside the bridge's control permissions")
    if entry.domain == "climate" and entry.platform != "bedjet":
        raise ValueError("Only BedJet climate control is available")
    if entry.domain in {"light", "switch"}:
        device = device_registry.async_get(hass).async_get(entry.device_id)
        if not device or "signify" not in (device.manufacturer or "").casefold():
            raise ValueError("This device is outside the bridge's Hue control permissions")
    if hass.states.get(entity_id) is None or hass.states.get(entity_id).state in {"unknown", "unavailable"}:
        raise ValueError("That device is unavailable")
    service_data = {"entity_id": entity_id}
    level = request.get("percent")
    temperature = request.get("temperature")
    if action != "temperature" and "temperature" in request:
        raise ValueError("A temperature requires the temperature action")
    if action == "fan_speed":
        if type(level) is not int or f"{level}%" not in hass.states.get(entity_id).attributes.get("fan_modes", ()):
            raise ValueError("Use a supported fan speed from 5% to 100% in 5% steps")
        service_data["fan_mode"] = f"{level}%"
    elif action in {"brightness", "volume"}:
        minimum = 1 if action == "brightness" else 0
        if type(level) is not int or not minimum <= level <= 100:
            raise ValueError(f"Provide a percentage from {minimum} to 100")
        if action == "brightness":
            service_data["brightness_pct"] = level
        else:
            service_data["volume_level"] = level / 100
    elif "percent" in request:
        raise ValueError("A percentage only applies to brightness or volume")
    if entry.domain == "climate":
        attributes = hass.states.get(entity_id).attributes
        if action == "temperature":
            minimum, maximum = attributes.get("min_temp"), attributes.get("max_temp")
            if type(temperature) not in {int, float} or minimum is None or maximum is None or not minimum <= temperature <= maximum:
                raise ValueError(f"Temperature must be between {minimum} and {maximum} {hass.config.units.temperature_unit}")
            service_data["temperature"] = temperature
        elif action != "fan_speed":
            mode = "off" if action == "turn_off" else action
            if mode not in attributes.get("hvac_modes", ()):
                raise ValueError("This BedJet does not support that mode")
            service_data["hvac_mode"] = mode
    if action in {"mute", "unmute"}:
        service_data["is_volume_muted"] = action == "mute"
    service = _CONTROLS[entry.domain][action]
    await hass.services.async_call(entry.domain, service, service_data, blocking=True)
    return {"action": action, "accepted": True, "device": describe(hass, entry)}


async def async_setup(hass, config):
    registry = entity_registry.async_get(hass)
    settings = config[DOMAIN]
    write_entities = frozenset(settings["write_entities"])
    read_entities = write_entities | frozenset(settings["read_entities"])
    camera_entity = settings.get("front_door_camera")

    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 4)
            if not raw or len(raw) > 14000:
                raise ValueError("Invalid request size")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("Invalid request")
            operation = request.get("operation")
            if operation == "status":
                result = search(hass, registry, read_entities, request)
            elif operation == "control":
                result = await asyncio.wait_for(control(hass, registry, write_entities, request), 10)
            elif operation in {"front_door_capability", "front_door_offer", "front_door_close", "front_door_snapshot"}:
                from .door_camera import camera_request

                if not camera_entity or not camera_entity.startswith("camera."):
                    raise ValueError("Front-door snapshots are disabled")
                result = await asyncio.wait_for(camera_request(hass, camera_entity, operation, request), 15)
            else:
                raise ValueError("Unknown operation")
            response = {"result": result}
        except (ValueError, HomeAssistantError) as error:
            response = {"error": str(error)[:220]}
        except (OSError, TimeoutError):
            response = {"error": "Home Assistant did not complete the request in time"}
        except Exception:
            _LOGGER.exception("Codie device request failed")
            response = {"error": "Home Assistant could not complete the request"}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        _SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handle, path=_SOCKET, limit=16384)
        os.chmod(_SOCKET, 0o660)
    except OSError:
        _LOGGER.exception("Cannot create the Codie device socket")
        return False

    async def stop(_event):
        server.close()
        await server.wait_closed()
        _SOCKET.unlink(missing_ok=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
