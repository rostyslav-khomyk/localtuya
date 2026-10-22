"""Battletron zones as normal HA colour lights.

Turning a zone on/off or changing its colour must paint that segment over UDP.
Screen sync temporarily owns the same output engine; manual state is retained.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from homeassistant.helpers.restore_state import RestoreEntity, RestoredExtraData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import color as color_util

from .battletron_zones import ZoneSender, run_zone_loop
from .const import CONF_COLOR, CONF_COLOR_MODE, CONF_LOCAL_KEY, DOMAIN
from .screen_layout import zone_counts

_LOGGER = logging.getLogger(__name__)

CONF_ZONES = "zones"
CONF_SCREEN_SYNC = "screen_sync"
CONF_OUTPUT_ENABLED = "screen_output_enabled"


def _sync(device: dict) -> dict:
    return device.get(CONF_SCREEN_SYNC) or {}


def _zones(device: dict) -> list[dict]:
    return list(_sync(device).get(CONF_ZONES) or [])


def device_has_zones(entry, device_id: str) -> bool:
    device = (entry.data.get(CONF_DEVICES) or {}).get(device_id) or {}
    return len(_zones(device)) > 0


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


def _light_entity(device: dict) -> dict | None:
    for entity in device.get("entities") or []:
        if str(entity.get("platform", "")).lower() == "light":
            return entity
    return None


def _power_dp(device: dict):
    ent = _light_entity(device)
    return ent.get("id") if ent else None


def _mode_dp(device: dict):
    """Work-mode DP (colour/music/scene). Music mode hides zone frames."""
    ent = _light_entity(device)
    if not ent:
        return None
    return ent.get(CONF_COLOR_MODE) or ent.get("color_mode")


def _color_dp(device: dict):
    """Solid colour DP — cleared to black when arming zones."""
    ent = _light_entity(device)
    if not ent:
        return None
    return ent.get(CONF_COLOR) or ent.get("color")


class ZonePaintHub:
    """One UDP :40001 painter for all strips that have zone lights on."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.sender = ZoneSender()
        self._run = None
        self._lock = asyncio.Lock()
        self._powered: set[str] = set()
        # device_id -> [{"on": bool, "rgb": (r,g,b)}, ...]  rgb is what goes on the wire
        self._state: dict[str, list[dict]] = {}
        self._last_logged: dict[str, tuple] = {}
        self.lights: dict[tuple[str, int], BattletronZoneLight] = {}
        self._stream_frames: dict[str, list[tuple[int, int, int]]] = {}
        self._stream_owner: dict[str, str] = {}
        self.stream = None
        self._stopping = False
        self.output_enabled = entry.data.get(CONF_OUTPUT_ENABLED, True)
        self._output_lock = asyncio.Lock()

    def assert_manual(self, device_id):
        if device_id in self._stream_owner:
            raise HomeAssistantError("Screen sync is active; pause it before changing this strip")

    async def begin_stream(self, owner, frames):
        async with self._lock:
            if self._stopping or not self.output_enabled:
                raise HomeAssistantError("ScreenEdges lighting is off or shutting down")
            if any(self._stream_owner.get(did, owner) != owner for did in frames):
                raise HomeAssistantError("Another screen-sync session owns this strip")
            for did, colors in frames.items():
                self._stream_owner[did] = owner
                self._stream_frames[did] = colors
        try:
            for did in frames:
                await self.apply(did)
            if not self.output_enabled or any(did not in self._powered for did in frames):
                raise HomeAssistantError("Could not power every selected strip")
        except BaseException:
            await self.end_stream(owner)
            raise
        self._notify_lights()

    def update_stream(self, owner, frames):
        if not self.output_enabled:
            raise HomeAssistantError("ScreenEdges lighting is off")
        for did, colors in frames.items():
            if self._stream_owner.get(did) != owner:
                raise HomeAssistantError("Screen-sync ownership expired")
            self._stream_frames[did] = colors

    async def end_stream(self, owner, restore=True):
        async with self._lock:
            ids = [did for did, current in self._stream_owner.items() if current == owner]
            for did in ids:
                self._stream_owner.pop(did)
                self._stream_frames.pop(did, None)
        if restore and not self._stopping and self.output_enabled:
            for did in ids:
                await self.apply(did)
        self._notify_lights()

    def _notify_lights(self):
        for light in self.lights.values():
            light.async_write_ha_state()

    def _device(self, device_id: str) -> dict | None:
        return (self.entry.data.get(CONF_DEVICES) or {}).get(device_id)

    def _ensure_state(self, device_id: str) -> list[dict]:
        device = self._device(device_id)
        zones = _zones(device) if device else []
        current = self._state.get(device_id)
        if current is None or len(current) != len(zones):
            # Preserve existing slots when growing/shrinking where possible.
            fresh = []
            for i in range(len(zones)):
                if current and i < len(current):
                    fresh.append(dict(current[i]))
                else:
                    fresh.append({"on": False, "rgb": (0, 0, 0)})
            current = fresh
            self._state[device_id] = current
        return current

    def set_zone(self, device_id: str, index: int, *, is_on: bool, rgb: tuple[int, int, int]):
        state = self._ensure_state(device_id)
        if 0 <= index < len(state):
            state[index] = {
                "on": bool(is_on),
                "rgb": (int(rgb[0]), int(rgb[1]), int(rgb[2])) if is_on else (0, 0, 0),
            }

    def zone_state(self, device_id: str, index: int) -> dict:
        state = self._ensure_state(device_id)
        if 0 <= index < len(state):
            return state[index]
        return {"on": False, "rgb": (0, 0, 0)}

    def painting_device_ids(self) -> list[str]:
        if not self.output_enabled:
            return []
        out = []
        for device_id, device in (self.entry.data.get(CONF_DEVICES) or {}).items():
            if not _zones(device):
                continue
            if device_id not in self._stream_frames and not any(s.get("on") for s in self._ensure_state(device_id)):
                continue
            if device.get(CONF_HOST) and device.get(CONF_LOCAL_KEY):
                out.append(device_id)
        return out

    def _colors_for(self, device_id: str, *, allow_all_off: bool = False):
        if not self.output_enabled:
            return [(0, 0, 0)] * 20, [1] * 20
        if device_id in self._stream_frames:
            return self._stream_frames[device_id], [1] * 20
        device = self._device(device_id)
        if not device:
            return None, None
        zones = _zones(device)
        state = self._ensure_state(device_id)
        colors = []
        lengths = []
        any_on = False
        for index, zone in enumerate(zones):
            cm = int(zone.get("cm") or 0)
            if cm <= 0:
                continue
            slot = state[index] if index < len(state) else {"on": False, "rgb": (0, 0, 0)}
            # Off zone → black on the wire so that segment stays dark.
            rgb = tuple(slot["rgb"]) if slot.get("on") else (0, 0, 0)
            if slot.get("on"):
                any_on = True
            colors.append(rgb)
            lengths.append(cm)
        if not colors or (not any_on and not allow_all_off):
            return None, None
        key = (device_id, tuple(colors), tuple(lengths))
        if self._last_logged.get(device_id) != key:
            self._last_logged[device_id] = key
            _LOGGER.info(
                "Zone colours %s %s counts=%s",
                device_id[:8],
                " ".join(str(c) for c in colors),
                zone_counts(lengths, segments=20),
            )
        return colors, zone_counts(lengths, segments=20)

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
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Zone painter stopped: %s", err)
        self.sender.close()

    async def apply(self, device_id: str):
        """Apply current zone state: power if needed, run painter, paint now."""
        async with self._lock:
            if self._stopping or not self.output_enabled:
                return
            device_ids = self.painting_device_ids()
            if device_id not in device_ids:
                # All zones off for this strip — black out, then drop from painter.
                await self.hass.async_add_executor_job(
                    self._paint_now, device_id, True
                )
                await self._power_off(device_id)
                remaining = [d for d in self.sender._devices if d != device_id and d in device_ids]
                if not device_ids:
                    await self._stop_loop()
                    self.sender = ZoneSender()
                elif set(remaining) != set(self.sender._devices):
                    await self._restart_locked(device_ids)
                self._powered.discard(device_id)
                self._last_logged.pop(device_id, None)
                return

            dropped = [d for d in self.sender._devices if d not in device_ids]
            for did in dropped:
                await self.hass.async_add_executor_job(self._paint_now, did, True)
                await self._power_off(did)
                self._powered.discard(did)
                self._last_logged.pop(did, None)

            if (not self._loop_running()) or set(device_ids) != set(self.sender._devices):
                await self._restart_locked(device_ids)
            elif device_id not in self._powered:
                await self._power_one(device_id)

            # Push one frame immediately so HA UI changes are not waiting on the tick.
            await self.hass.async_add_executor_job(self._paint_now, device_id, False)

    def _paint_now(self, device_id: str, allow_all_off: bool = False) -> None:
        colors, counts = self._colors_for(device_id, allow_all_off=allow_all_off)
        if not colors:
            return
        if device_id not in self.sender._devices:
            return
        if not self.sender._devices[device_id].get("session"):
            # Negotiate once so the first HA click is not blank.
            self.sender.negotiate(device_id)
            for _ in range(20):
                finished = self.sender.pump()
                if finished == device_id:
                    break
                time.sleep(0.05)
        self.sender.send_colors(device_id, colors, counts)
        if allow_all_off:
            # A few more black frames so the strip does not keep the last colour.
            for _ in range(3):
                time.sleep(0.05)
                self.sender.send_colors(device_id, colors, counts)

    async def refresh(self):
        async with self._lock:
            ids = self.painting_device_ids()
            if not ids:
                await self._stop_loop()
                self.sender = ZoneSender()
                self._powered.clear()
                self._last_logged.clear()
                return
            await self._restart_locked(ids)

    async def _restart_locked(self, device_ids: list[str]):
        await self._stop_loop()
        await asyncio.sleep(0.1)
        self.sender = ZoneSender()
        # Socket/session was dropped — must re-assert TCP power. Leaving ids in
        # `_powered` made `_power_one` no-op after SE pause/resume or dual-strip
        # restart, so UDP negotiated while LEDs stayed dark (DP20 off / DP24 black).
        self._powered.clear()
        for did in device_ids:
            device = self._device(did)
            if not device:
                continue
            key, host = device.get(CONF_LOCAL_KEY), device.get(CONF_HOST)
            if key and host:
                self.sender.add(did, key, host)
        device_ids = list(self.sender._devices)
        if not device_ids:
            return
        for did in device_ids:
            await self._power_one(did)
        await asyncio.sleep(0.25)
        self._run = self.entry.async_create_background_task(
            self.hass,
            run_zone_loop(self.sender, device_ids, self._colors_for, interval=0.1),
            "battletron-zone-lights",
        )

    async def _power_one(self, device_id: str):
        """Power the strip on for zone UDP, clear solid colour, hold TCP closed.

        Stop-sync turns the strip off over TCP. Without powering back on here,
        UDP frames are accepted but the LEDs stay dark. Colour DP is set black
        so the last orange solid colour cannot flash through.
        """
        if device_id in self._powered:
            return
        device = self._device(device_id)
        tuya = _tuya_device(self.hass, self.entry, device_id)
        power = _power_dp(device) if device else None
        if tuya is None or power is None:
            return
        mode = _mode_dp(device) if device else None
        color = _color_dp(device) if device else None
        try:
            await tuya.async_power_for_zones(power, mode, color)
            self._powered.add(device_id)
            _LOGGER.info(
                "Zone strip %s powered (mode_dp=%s color_dp=%s→black)",
                device_id[:8],
                mode,
                color,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not power zone strip %s: %s", device_id[:8], err)

    async def _power_off(self, device_id: str):
        """Best-effort TCP power off after black UDP frames; keep TCP held."""
        device = self._device(device_id)
        tuya = _tuya_device(self.hass, self.entry, device_id)
        power = _power_dp(device) if device else None
        if tuya is None or power is None:
            return
        try:
            tuya._zone_tcp_window = True
            tuya._hold_tcp = False
            await tuya.async_connect()
            if tuya.connected:
                dp = int(power) if str(power).isdigit() else power
                await tuya.set_dps({dp: False})
                await asyncio.sleep(0.15)
                _LOGGER.info("Zone strip %s powered off", device_id[:8])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not power off zone strip %s: %s", device_id[:8], err)
        finally:
            tuya._zone_tcp_window = False
            tuya._pending_status = {}
            # Hold closed — release_tcp() reconnects and can push DP24 orange.
            await tuya.async_hold_tcp()

    async def _blackout_all(self):
        async with self._lock:
            for did in list(self.sender._devices):
                await self.hass.async_add_executor_job(self._paint_now, did, True)
            await self._stop_loop()
            self.sender.close()
            for did, device in self.entry.data.get(CONF_DEVICES, {}).items():
                if _zones(device):
                    await self._power_off(did)
            self._powered.clear()
            self.sender = ZoneSender()

    async def restore_output(self):
        if self.output_enabled:
            await self.refresh()
        else:
            await self._blackout_all()

    async def set_output_enabled(self, enabled):
        from .screen_sync import _save

        async with self._output_lock:
            async with self._lock:
                if self._stopping:
                    raise HomeAssistantError("Zone output is shutting down")
                if self.output_enabled == enabled:
                    return
                # Latch and persist before awaiting any stream/socket cleanup.
                _save(self.hass, self.entry, {**self.entry.data, CONF_OUTPUT_ENABLED: enabled})
                self.output_enabled = enabled
                self._notify_lights()
            if not enabled:
                if self.stream is not None:
                    await self.stream.close(restore=False)
                async with self._lock:
                    self._stream_owner.clear()
                    self._stream_frames.clear()
                    self._notify_lights()
                await self._blackout_all()
            else:
                await self.refresh()

    async def stop_device(self, device_id: str):
        self.assert_manual(device_id)
        for slot in self._ensure_state(device_id):
            slot["on"] = False
            slot["rgb"] = (0, 0, 0)
        await self.apply(device_id)

    async def stop(self):
        self._stopping = True
        if self.stream is not None:
            await self.stream.close(restore=False)
        async with self._lock:
            await self._stop_loop()
            self.sender = ZoneSender()
            self._powered.clear()
            self._last_logged.clear()


class BattletronZoneLight(LightEntity, RestoreEntity):
    """One segment of a Battletron strip — standard RGB light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB

    def __init__(self, entry, device_id: str, device: dict, index: int):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        self._attr_name = f"Zone {index + 1}"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_light"
        self._attr_device_info = _device_info(device_id, device)
        self._is_on = False
        # Full-brightness colour; brightness scales it for the wire.
        self._rgb = (255, 255, 255)
        self._brightness = 255

    @property
    def is_on(self) -> bool:
        hub = self._hub() if self.hass is not None else None
        return self._is_on and (hub is None or hub.output_enabled)

    @property
    def brightness(self) -> int | None:
        return self._brightness if self._is_on else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb if self._is_on else None

    def _hub(self) -> ZonePaintHub | None:
        return _hubs(self.hass).get(self._entry.entry_id)

    def _wire_rgb(self) -> tuple[int, int, int]:
        scale = self._brightness / 255.0
        return tuple(min(255, max(0, int(c * scale))) for c in self._rgb)

    async def async_turn_on(self, **kwargs: Any) -> None:
        hub = self._hub()
        if hub:
            hub.assert_manual(self._device_id)
        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            self._rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        elif ATTR_HS_COLOR in kwargs:
            hs = kwargs[ATTR_HS_COLOR]
            self._rgb = color_util.color_hs_to_RGB(float(hs[0]), float(hs[1]))
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = int(kwargs[ATTR_BRIGHTNESS])
        self._is_on = True
        hub = self._hub()
        if hub:
            hub.set_zone(self._device_id, self._index, is_on=True, rgb=self._wire_rgb())
            await hub.apply(self._device_id)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        hub = self._hub()
        if hub:
            hub.assert_manual(self._device_id)
        self._is_on = False
        hub = self._hub()
        if hub:
            hub.set_zone(self._device_id, self._index, is_on=False, rgb=(0, 0, 0))
            # apply() paints black for this slot (or black+power-off if last zone).
            await hub.apply(self._device_id)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        hub = self._hub()
        if not hub:
            return
        hub.lights[(self._device_id, self._index)] = self
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            saved = extra.as_dict()
            self._is_on = saved["on"]
            self._rgb = tuple(saved["rgb"])
            self._brightness = saved["brightness"]
        elif (last := await self.async_get_last_state()) is not None:
            self._is_on = last.state == "on"
            self._rgb = tuple(last.attributes.get("rgb_color") or (255, 255, 255))
            self._brightness = last.attributes.get("brightness") or 255
        hub.set_zone(self._device_id, self._index, is_on=self._is_on, rgb=self._wire_rgb())

    @property
    def extra_restore_state_data(self):
        # Preserve desired on/off and remembered color even while the master is off.
        return RestoredExtraData({"on": self._is_on, "rgb": list(self._rgb), "brightness": self._brightness})

    @property
    def extra_state_attributes(self):
        hub = self._hub()
        return {"screen_sync_active": bool(hub and self._device_id in hub._stream_owner),
                "screen_output_enabled": bool(hub and hub.output_enabled)}

    async def async_will_remove_from_hass(self):
        if hub := self._hub():
            hub.lights.pop((self._device_id, self._index), None)
        await super().async_will_remove_from_hass()


def zones_painting(hass, entry, device_id: str) -> bool:
    hub = _hubs(hass).get(entry.entry_id)
    if not hub:
        return False
    return (not hub.output_enabled and device_has_zones(entry, device_id)) or device_id in hub._stream_owner or any(s.get("on") for s in hub._ensure_state(device_id))


async def async_release_zones(device) -> None:
    hub = _hubs(device.hass).get(device._entry.entry_id)
    if hub:
        await hub.stop_device(device.id)


async def async_add_zone_lights(hass, entry, async_add_entities):
    entities = [
        BattletronZoneLight(entry, device_id, device, index)
        for device_id, device in entry.data.get(CONF_DEVICES, {}).items()
        for index in range(len(_zones(device)))
    ]
    if entities:
        async_add_entities(entities)


async def async_start_zone_hub(hass, entry) -> ZonePaintHub:
    hub = ZonePaintHub(hass, entry)
    _hubs(hass)[entry.entry_id] = hub

    def _forget():
        # Mark synchronously before HA cancels all entry background tasks.
        hub._stopping = True
        _hubs(hass).pop(entry.entry_id, None)
        return hub.stop()

    entry.async_on_unload(_forget)
    return hub
