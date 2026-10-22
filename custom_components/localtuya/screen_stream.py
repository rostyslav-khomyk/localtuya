"""Authenticated stream control; HA remains the only owner of strip UDP output."""

import asyncio
import ipaddress
import json
import logging
import time

from aiohttp import ClientWSTimeout, WSMsgType, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .granular import EDGES, SAMPLES, decode_frame, map_strip
from .screen_layout import zone_counts
from .zone_lights import _hubs, _zones

_LOGGER = logging.getLogger(__name__)
FRAME_TIMEOUT = 5.0


class StreamController:
    def __init__(self, hub, session):
        self.hub = hub
        self.session = session
        self.owner = None
        self._task = None
        self._spec = None
        self.error = None

    async def start(self, owner, url, token, bindings):
        if not self.hub.output_enabled:
            raise HomeAssistantError("ScreenEdges lighting is off")
        spec = (owner, url, token, bindings)
        if self._task and not self._task.done():
            if self.owner != owner:
                raise ValueError("Another screen-sync client is active")
            if self._spec == spec:
                return
        await self.close()
        self.owner, self._spec, self.error = owner, spec, None
        ready = asyncio.Event()
        self._task = self.hub.entry.async_create_background_task(
            self.hub.hass, self._run(owner, url, token, bindings, ready), "granular-screen-stream"
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=15)
            if self.error or not any(current == owner for current in self.hub._stream_owner.values()):
                raise ValueError(self.error or "Screen stream closed before ownership was acquired")
        except BaseException:
            await self.close()
            raise

    async def _run(self, owner, url, token, bindings, ready):
        try:
            async with self.session.ws_connect(url, max_msg_size=65536, timeout=ClientWSTimeout(ws_close=1), compress=0) as ws:
                await ws.send_json({"command": "authorize", "subcommand": "login", "token": token, "tan": 1})
                reply = await ws.receive_json(timeout=5)
                if not isinstance(reply, dict) or reply.get("success") is not True:
                    raise ValueError("Screen source rejected authentication")
                await ws.send_json({"command": "ledcolors", "subcommand": "ledstream-start", "tan": 2})
                last_frame = time.monotonic()
                acquired = False
                while True:
                    remaining = FRAME_TIMEOUT - (time.monotonic() - last_frame)
                    if remaining <= 0:
                        raise TimeoutError("Screen frames stopped")
                    message = await ws.receive(timeout=remaining)
                    if message.type != WSMsgType.TEXT:
                        raise ValueError("Screen source disconnected")
                    edges = decode_frame(message.json())
                    if edges is None:
                        continue
                    frames = {
                        did: map_strip([int(zone["cm"]) for zone in _zones(self.hub._device(did))], maps, edges)
                        for did, maps in bindings.items()
                    }
                    if not acquired:
                        await self.hub.begin_stream(owner, frames)
                        acquired = True
                        ready.set()
                    else:
                        self.hub.update_stream(owner, frames)
                    last_frame = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Never log source payloads, authentication tokens, or device keys.
            self.error = "Screen source failed validation or disconnected" if not isinstance(err, TimeoutError) else "Screen frames timed out"
            _LOGGER.warning("Granular screen stream stopped (%s)", type(err).__name__)
        finally:
            await self.hub.end_stream(owner)
            ready.set()

    async def close(self, restore=True):
        task, self._task = self._task, None
        if not restore and self.owner:
            await self.hub.end_stream(self.owner, restore=False)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.owner = None
        self._spec = None


class ScreenStreamView(HomeAssistantView):
    url = "/api/localtuya/screen_sync"
    name = "api:localtuya:screen_sync"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self._lock = asyncio.Lock()

    def _zones(self):
        rows = []
        for hub in _hubs(self.hass).values():
            for (did, index), light in hub.lights.items():
                device = hub._device(did)
                zones = _zones(device)
                if not device or not 0 <= index < len(zones) or not light.entity_id:
                    continue
                counts = zone_counts([int(zone["cm"]) for zone in zones], segments=SAMPLES)
                rows.append((hub, did, index, {
                    "entity_id": light.entity_id, "name": light.name or light.entity_id,
                    "device_name": device.get("friendly_name") or device.get("name") or did,
                    "cm": int(zones[index]["cm"]), "sections": counts[index],
                    "screen_sync_active": did in hub._stream_owner,
                    "output_enabled": hub.output_enabled,
                }))
        return rows

    async def get(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "Administrator access required"}, status_code=403)
        return self.json({
            "protocol_version": 1, "samples_per_edge": SAMPLES,
            "zones": [row[3] for row in self._zones()],
            "owners": sorted({owner for hub in _hubs(self.hass).values() for owner in hub._stream_owner.values()}),
        })

    async def post(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "Administrator access required"}, status_code=403)
        try:
            if request.content_length and request.content_length > 16384:
                raise ValueError("Stream request too large")
            raw = await request.read()
            if len(raw) > 16384:
                raise ValueError("Stream request too large")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            owner = body.get("owner")
            if not isinstance(owner, str) or not 1 <= len(owner) <= 64:
                raise ValueError("Missing screen-sync owner")
            async with self._lock:
                if body.get("action") == "stop":
                    for hub in _hubs(self.hass).values():
                        if hub.stream and hub.stream.owner == owner:
                            await hub.stream.close()
                    return self.json({"success": True})
                if body.get("action") != "start":
                    raise ValueError("Use start or stop")
                port, token, mappings = body.get("port"), body.get("token"), body.get("mappings")
                if type(port) is not int or not 1024 <= port <= 65535:
                    raise ValueError("Invalid screen source port")
                if not isinstance(token, str) or not 16 <= len(token) <= 256:
                    raise ValueError("Invalid screen source token")
                if not isinstance(mappings, list) or not 1 <= len(mappings) <= 64:
                    raise ValueError("Select at least one zone")
                rows = {row[3]["entity_id"]: row for row in self._zones()}
                seen, bindings, selected_hub = set(), {}, None
                for mapping in mappings:
                    if not isinstance(mapping, dict):
                        raise ValueError("Invalid mapping")
                    entity = mapping.get("entity_id")
                    if not isinstance(entity, str) or entity not in rows or entity in seen:
                        raise ValueError("Unknown or duplicate zone")
                    edge, reverse = mapping.get("edge"), mapping.get("reverse", False)
                    if edge not in EDGES or type(reverse) is not bool:
                        raise ValueError("Invalid screen edge or direction")
                    hub, did, index, _ = rows[entity]
                    if selected_hub is not None and hub is not selected_hub:
                        raise ValueError("All selected strips must share one LocalTuya config entry")
                    selected_hub = hub
                    seen.add(entity)
                    bindings.setdefault(did, {})[index] = (edge, reverse)
                if not selected_hub.output_enabled:
                    return self.json({"error": "ScreenEdges lighting is off", "output_enabled": False}, status_code=409)
                # Use only the authenticated caller's IP, never a supplied URL/host.
                source = ipaddress.ip_address(request.remote)
                host = f"[{source}]" if source.version == 6 else str(source)
                if selected_hub.stream is None:
                    selected_hub.stream = StreamController(selected_hub, async_get_clientsession(self.hass))
                await selected_hub.stream.start(owner, f"ws://{host}:{port}", token, bindings)
                return self.json({"success": True})
        except (ValueError, TypeError, HomeAssistantError):
            return self.json({"error": "Invalid stream request, unavailable source, or conflicting ownership"}, status_code=400)
        except TimeoutError:
            return self.json({"error": "Screen source did not become ready"}, status_code=504)
