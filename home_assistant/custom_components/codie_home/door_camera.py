"""Capture a fresh frame from the single authorized Nest doorbell."""
from asyncio import get_running_loop
from base64 import b64encode
from uuid import UUID
from datetime import datetime, timezone

from homeassistant.components.camera import async_get_image, get_camera_from_entity_id
from homeassistant.components.nest.camera import NestRTSPEntity, NestWebRTCEntity

async def camera_request(hass, entity_id, operation, request):
    camera = get_camera_from_entity_id(hass, entity_id)
    if not camera.available:
        raise ValueError("The front-door camera is unavailable")
    if isinstance(camera, NestWebRTCEntity):
        if operation == "front_door_capability":
            return {"protocol": "webrtc"}
        supplied = request.get("session_id")
        if not isinstance(supplied, str):
            raise ValueError("A valid camera session ID is required")
        session_id = str(UUID(supplied))
        if operation == "front_door_close":
            camera.close_webrtc_session(session_id)
            return {"closed": True}
        offer = request.get("sdp")
        if operation != "front_door_offer" or not isinstance(offer, str) or not 100 < len(offer) < 12000:
            raise ValueError("A valid WebRTC offer is required")
        answer = None

        def receive(message):
            nonlocal answer
            answer = getattr(message, "answer", None)

        try:
            await camera.async_handle_async_webrtc_offer(offer, session_id, receive)
            if not answer:
                raise ValueError("Nest returned no WebRTC answer")
            get_running_loop().call_later(30, camera.close_webrtc_session, session_id)
        except BaseException:
            camera.close_webrtc_session(session_id)
            raise
        return {"sdp": answer}
    if not isinstance(camera, NestRTSPEntity):
        raise ValueError("The configured front-door camera is not a supported Nest device")
    if operation == "front_door_capability":
        return {"protocol": "rtsp"}
    if operation != "front_door_snapshot":
        raise ValueError("Unexpected operation for an RTSP camera")
    image = await async_get_image(hass, entity_id, timeout=12, width=640)
    if not image.content or len(image.content) > 1024 * 1024:
        raise ValueError("The camera did not return a usable still")
    return {
        "image": b64encode(image.content).decode("ascii"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
