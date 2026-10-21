"""Battletron strip chopped into standard HA color lights (one per zone).

Each zone is a normal ``light`` entity (HS/RGB). Turning a zone on updates the
shared UDP zone painter for that strip. Screen Edges (or HomeKit / Matter)
only talks to these lights — never to Hyperion or a special sync switch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.const import CONF_DEVICES, CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import color as color_util

from .battletron_zones import ZoneSender, run_zone_loop
from .const import CONF_LOCAL_KEY, DOMAIN
from .screen_layout import zone_counts

_LOGGER = logging.getLogger(__name__)

CONF_ZONES = "zones"
CONF_SCREEN_SYNC = "screen_sync"


def _sync(device: dict) -> dict:
    return device.get(CONF_SCREEN_SYNC) or {}


def _zones(device: dict) -> list[dict]:
    return list(_sync(device).get(CONF_ZONES) or [])


def _device_info(device_id: str, device: dict) -> DeviceInfo:
    name = device.get("friendly_name") or device.get("name") or device_id
    return DeviceInfo(identifiers={(DOMAIN, f"local_{device_id}")}, name=name)


def _hubs(hass) -> dict:
    return hass.data[DOMAIN].setdefault("zone_hubs", {})


def _tuya_device(hass, entry, device_id: str):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        return None
    for device in data.devices.values():
        if getattr(device, "id", None) == device_id:
            return device
    return None


def _light_dps(device: dict):
    for entity in device.get("entities") or []:
        if str(entity.get("platform", "")).lower() == "light":
            return entity.get("id"), entity.get("color_mode")
    return None, None


class ZonePaintHub:
    """One UDP painter for the config entry. Several strips share port 40001."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.sender = ZoneSender()
        self._run = None
        self._lock = asyncio.Lock()
        # device_id -> list of {"on": bool, "rgb": (r,g,b)} aligned with zones[]
        self._state: dict[str, list[dict]] = {}

    def _device(self, device_id: str) -> dict | None:
        return (self.entry.data.get(CONF_DEVICES) or {}).get(device_id)

    def _ensure_state(self, device_id: str) -> list[dict]:
        device = self._device(device_id)
        zones = _zones(device) if device else []
        current = self._state.get(device_id)
        if current is None or len(current) != len(zones):
            current = [{"on": False, "rgb": (0, 0, 0)} for _ in zones]
            self._state[device_id] = current
        return current

    def set_zone(self, device_id: str, index: int, *, is_on: bool, rgb: tuple[int, int, int]):
        state = self._ensure_state(device_id)
        if index < 0 or index >= len(state):
            return
        state[index] = {"on": is_on, "rgb": tuple(int(c) for c in rgb[:3])}

    def zone_state(self, device_id: str, index: int) -> dict:
        state = self._ensure_state(device_id)
        if index < 0 or index >= len(state):
            return {"on": False, "rgb": (0, 0, 0)}
        return state[index]

    def painting_device_ids(self) -> list[str]:
        active = []
        for device_id, device in (self.entry.data.get(CONF_DEVICES) or {}).items():
            if not _zones(device):
                continue
            state = self._ensure_state(device_id)
            if not any(slot.get("on") for slot in state):
                continue
            if device.get(CONF_HOST) and device.get(CONF_LOCAL_KEY):
                active.append(device_id)
        return active

    def _colors_for(self, device_id: str):
        device = self._device(device_id)
        if not device:
            return None, None
        all_zones = _zones(device)
        if not all_zones:
            return None, None
        state = self._ensure_state(device_id)
        colors = []
        lengths = []
        any_on = False
        for index, zone in enumerate(all_zones):
            cm = int(zone.get("cm") or 0)
            if cm <= 0:
                continue
            slot = state[index] if index < len(state) else {"on": False, "rgb": (0, 0, 0)}
            rgb = slot["rgb"] if slot.get("on") else (0, 0, 0)
            if slot.get("on"):
                any_on = True
            colors.append(rgb)
            lengths.append(cm)
        if not any_on or not colors:
            return None, None
        return colors, zone_counts(lengths)

    def _loop_running(self) -> bool:
        return self._run is not None and not self._run.done()

    async def _stop_loop(self) -> None:
        task = self._run
        self._run = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as err:  # noqa: BLE001 — prior painter may have crashed
            _LOGGER.debug("Zone painter stopped with %s", err)
        self.sender.close()

    async def ensure_painting(self, device_id: str, *, power: bool = False):
        """Start or keep the UDP painter. Colour updates do not restart it."""
        async with self._lock:
            device_ids = self.painting_device_ids()
            if not device_ids:
                await self._stop_loop()
                self.sender = ZoneSender()
                return
            if self._loop_running() and device_id in self.sender._devices:
                return
            await self._restart_locked(power=power or device_id in device_ids)

    async def refresh(self, *, power: bool = False):
        """Rebuild the painter from current zone light state."""
        async with self._lock:
            await self._restart_locked(power=power)

    async def _restart_locked(self, *, power: bool = False):
        await self._stop_loop()
        await asyncio.sleep(0.1)
        self.sender = ZoneSender()
        device_ids = self.painting_device_ids()
        for did in device_ids:
            device = self._device(did)
            if not device:
                continue
            key = device.get(CONF_LOCAL_KEY)
            host = device.get(CONF_HOST)
            if key and host:
                self.sender.add(did, key, host)
        device_ids = list(self.sender._devices)
        if not device_ids:
            return
        if power:
            await self._power_devices(device_ids)
            await asyncio.sleep(0.4)
        else:
            for did in device_ids:
                tuya = _tuya_device(self.hass, self.entry, did)
                if tuya is not None:
                    await tuya.async_hold_tcp()
        self._run = self.entry.async_create_background_task(
            self.hass,
            run_zone_loop(self.sender, device_ids, self._colors_for),
            "battletron-zone-lights",
        )

    async def _power_devices(self, device_ids: list[str]):
        for device_id in device_ids:
            device = self._device(device_id)
            tuya = _tuya_device(self.hass, self.entry, device_id)
            if tuya is None or device is None:
                continue
            power, mode = _light_dps(device)
            await tuya.async_power_for_zones(power, mode)

    async def stop_device(self, device_id: str):
        """Parent strip light took over — clear zones and release TCP."""
        state = self._ensure_state(device_id)
        for slot in state:
            slot["on"] = False
            slot["rgb"] = (0, 0, 0)
        await self.refresh()
        tuya = _tuya_device(self.hass, self.entry, device_id)
        if tuya is not None:
            tuya._pending_status = {}
            await tuya.async_release_tcp()

    async def stop(self):
        async with self._lock:
            await self._stop_loop()
            self.sender = ZoneSender()


class BattletronZoneLight(LightEntity):
    """One colour segment of a Battletron strip."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS

    def __init__(self, entry, device_id: str, device: dict, index: int):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        zones = _zones(device)
        edge = (zones[index].get("edge") if index < len(zones) else None) or f"zone {index + 1}"
        self._attr_name = f"Zone {index + 1} ({edge})"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_light"
        self._attr_device_info = _device_info(device_id, device)
        self._is_on = False
        self._hs = (0.0, 0.0)
        self._brightness = 255

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness if self._is_on else None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self._hs if self._is_on else None

    def _hub(self) -> ZonePaintHub | None:
        return _hubs(self.hass).get(self._entry.entry_id)

    def _rgb(self) -> tuple[int, int, int]:
        r, g, b = color_util.color_hs_to_RGB(self._hs[0], self._hs[1])
        scale = (self._brightness or 255) / 255.0
        return (int(r * scale), int(g * scale), int(b * scale))

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_HS_COLOR in kwargs:
            hs = kwargs[ATTR_HS_COLOR]
            self._hs = (float(hs[0]), float(hs[1]))
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = int(kwargs[ATTR_BRIGHTNESS])
        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            self._hs = color_util.color_RGB_to_hs(rgb[0], rgb[1], rgb[2])
            self._brightness = max(int(rgb[0]), int(rgb[1]), int(rgb[2]), 1)
        was_off = not self._is_on
        self._is_on = True
        hub = self._hub()
        if hub is None:
            _LOGGER.warning("Zone hub is not ready for %s", self._device_id)
        else:
            hub.set_zone(self._device_id, self._index, is_on=True, rgb=self._rgb())
            await hub.ensure_painting(self._device_id, power=was_off)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        hub = self._hub()
        if hub:
            hub.set_zone(self._device_id, self._index, is_on=False, rgb=(0, 0, 0))
            device = (self._entry.data.get(CONF_DEVICES) or {}).get(self._device_id) or {}
            still = any(
                hub.zone_state(self._device_id, i).get("on")
                for i in range(len(_zones(device)))
            )
            if not still:
                await hub.refresh()
                tuya = _tuya_device(self.hass, self._entry, self._device_id)
                if tuya is not None:
                    tuya._pending_status = {}
                    await tuya.async_release_tcp()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        hub = self._hub()
        if not hub:
            return
        slot = hub.zone_state(self._device_id, self._index)
        if slot.get("on"):
            self._is_on = True
            rgb = slot.get("rgb") or (0, 0, 0)
            self._hs = color_util.color_RGB_to_hs(*rgb)
            self._brightness = max(rgb[0], rgb[1], rgb[2], 1)


def zones_painting(hass, entry, device_id: str) -> bool:
    hub = _hubs(hass).get(entry.entry_id)
    if not hub:
        return False
    try:
        state = hub._ensure_state(device_id)
    except Exception:  # noqa: BLE001
        return False
    return any(slot.get("on") for slot in state)


async def async_release_zones(device) -> None:
    """Parent strip light is taking over."""
    hub = _hubs(device.hass).get(device._entry.entry_id)
    if hub:
        await hub.stop_device(device.id)


async def async_add_zone_lights(hass, entry, async_add_entities):
    entities = []
    for device_id, device in entry.data.get(CONF_DEVICES, {}).items():
        for index in range(len(_zones(device))):
            entities.append(BattletronZoneLight(entry, device_id, device, index))
    if entities:
        async_add_entities(entities)


async def async_start_zone_hub(hass, entry) -> ZonePaintHub:
    hub = ZonePaintHub(hass, entry)
    _hubs(hass)[entry.entry_id] = hub

    def _forget():
        _hubs(hass).pop(entry.entry_id, None)
        return hub.stop()

    entry.async_on_unload(_forget)
    return hub
