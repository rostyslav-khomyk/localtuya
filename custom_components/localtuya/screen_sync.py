"""Follow a Hyperion screen-edge server and paint Battletron zone strips.

Several strips can share one server. Each strip has its own zone lengths.
Solid color, scenes, and music stay on the normal light entity. Turning
screen sync on closes that color link and leaves the saved color alone.
"""

import asyncio
import logging

import aiohttp
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_DEVICES, CONF_HOST, UnitOfLength
from homeassistant.helpers.device_registry import DeviceInfo

from .battletron_zones import ZoneSender, run_zone_loop
from .const import CONF_LOCAL_KEY, DOMAIN
from .screen_layout import EDGES, zone_counts

_LOGGER = logging.getLogger(__name__)

CONF_SCREEN_SERVERS = "screen_servers"
CONF_SCREEN_SYNC = "screen_sync"
CONF_LOCAL_ONLY = "local_only"


def _sync(device):
    return device.get(CONF_SCREEN_SYNC) or {}


def _servers(entry):
    return list(entry.data.get(CONF_SCREEN_SERVERS) or [])


def _server(entry, name):
    for item in _servers(entry):
        if item.get("name") == name:
            return item
    return None


class EdgeCache:
    def __init__(self):
        self.colors = {name: (0, 0, 0) for name in EDGES}
        self.seen = False
        self.task = None


class ScreenHub:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.edges = {}
        self.sender = ZoneSender()
        self._run = None

    def refresh_devices(self):
        self.sender.close()
        self.sender = ZoneSender()
        for dev_id, device in self.entry.data.get(CONF_DEVICES, {}).items():
            sync = _sync(device)
            if not sync.get("enabled"):
                continue
            if not _server(self.entry, sync.get("server")):
                continue
            host = device.get(CONF_HOST)
            key = device.get(CONF_LOCAL_KEY)
            if host and key:
                self.sender.add(dev_id, key, host)

    async def start(self):
        self.refresh_devices()
        used = {
            _sync(device).get("server")
            for device in self.entry.data.get(CONF_DEVICES, {}).values()
            if _sync(device).get("enabled")
        }
        for server in _servers(self.entry):
            name = server.get("name")
            if name not in used:
                continue
            cache = self.edges.setdefault(name, EdgeCache())
            if cache.task is None or cache.task.done():
                cache.task = self.entry.async_create_background_task(
                    self.hass,
                    self._follow(server, cache),
                    f"screen-edges-{name}",
                )
        device_ids = list(self.sender._devices)
        if not device_ids:
            return
        self._run = self.entry.async_create_background_task(
            self.hass,
            run_zone_loop(self.sender, device_ids, self._colors_for),
            "battletron-zones",
        )

    async def restart_zones(self):
        """Apply a server or zone change without dropping the screen connection."""
        if self._run:
            self._run.cancel()
            self._run = None
        self.refresh_devices()
        device_ids = list(self.sender._devices)
        if not device_ids:
            self.sender.close()
            return
        self._run = self.entry.async_create_background_task(
            self.hass,
            run_zone_loop(self.sender, device_ids, self._colors_for),
            "battletron-zones",
        )

    async def stop(self):
        for cache in self.edges.values():
            if cache.task:
                cache.task.cancel()
        if self._run:
            self._run.cancel()
        self.sender.close()

    def _colors_for(self, device_id):
        device = self.entry.data[CONF_DEVICES][device_id]
        sync = _sync(device)
        cache = self.edges.get(sync.get("server"))
        if not cache or not cache.seen:
            return None, None
        zones = [zone for zone in sync.get("zones", []) if int(zone.get("cm") or 0) > 0]
        if not zones:
            return None, None
        colors = []
        for zone in zones:
            colors.append(cache.colors.get(zone["edge"], (0, 0, 0)))
        return colors, zone_counts([zone["cm"] for zone in zones])

    async def _follow(self, server, cache):
        url = f"ws://{server['host']}:{int(server.get('port') or 8090)}"
        token = server.get("token") or ""
        while True:
            try:
                if not token:
                    raise RuntimeError("screen server has no token")
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, compress=0) as ws:
                        await ws.send_json(
                            {
                                "command": "authorize",
                                "subcommand": "login",
                                "token": token,
                                "tan": 1,
                            }
                        )
                        login = await ws.receive_json(timeout=5)
                        if not login.get("success"):
                            raise RuntimeError(login.get("error") or "login was rejected")
                        await ws.send_json(
                            {"command": "ledcolors", "subcommand": "ledstream-start", "tan": 2}
                        )
                        _LOGGER.info("Screen server %s is streaming", server.get("name"))
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            body = msg.json()
                            if body.get("command") != "ledcolors-ledstream-update":
                                continue
                            payload = body.get("data") or body.get("result") or {}
                            leds = payload.get("leds") or []
                            for index, name in enumerate(EDGES):
                                if index >= len(leds):
                                    break
                                led = leds[index]
                                rgb = led.get("RGB Value") if isinstance(led, dict) else led
                                cache.colors[name] = tuple(int(v) for v in rgb[:3])
                            cache.seen = True
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Screen server %s: %s", server.get("name"), err)
                await asyncio.sleep(5)


def _device_info(device_id, device):
    name = device.get("friendly_name") or device.get("name") or device_id
    return DeviceInfo(identifiers={(DOMAIN, f"local_{device_id}")}, name=name)


def _zones(device):
    return list(_sync(device).get("zones") or [])


def _save(hass, entry):
    hass.config_entries.async_update_entry(entry, data=dict(entry.data))


async def _persist_and_reload(hass, entry):
    _save(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)


def _poke(tuya):
    """Ask the strip's entities to read the switches again."""
    if tuya is None:
        return
    for entity in list(getattr(tuya, "_entities", ())):
        entity.schedule_update_ha_state()


def _tuya_device(hass, entry, device_id):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        return None
    for device in data.devices.values():
        if getattr(device, "id", None) == device_id:
            return device
    return None


class ScreenSyncSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Screen sync"

    def __init__(self, entry, device_id, device):
        self._entry = entry
        self._device_id = device_id
        self._attr_unique_id = f"local_{device_id}_screen_sync"
        self._attr_device_info = _device_info(device_id, device)

    @property
    def is_on(self):
        device = self._entry.data[CONF_DEVICES].get(self._device_id) or {}
        return bool(_sync(device).get("enabled"))

    async def async_turn_on(self, **kwargs):
        """Close the color link and follow the screen. The saved color stays stored."""
        device = self._entry.data[CONF_DEVICES][self._device_id]
        sync = _sync(device)
        sync["enabled"] = True
        device["screen_sync"] = sync
        _save(self.hass, self._entry)
        tuya = _tuya_device(self.hass, self._entry, self._device_id)
        if tuya is not None:
            await tuya.async_hold_tcp()
        hub = _hubs(self.hass).get(self._entry.entry_id)
        if hub:
            await hub.restart_zones()
        self.schedule_update_ha_state()
        _poke(tuya)

    async def async_turn_off(self, **kwargs):
        """Stop screen sync. Leave the light off. Do not rewrite its color."""
        device = self._entry.data[CONF_DEVICES][self._device_id]
        sync = _sync(device)
        sync["enabled"] = False
        device["screen_sync"] = sync
        _save(self.hass, self._entry)
        hub = _hubs(self.hass).get(self._entry.entry_id)
        if hub:
            await hub.restart_zones()
        tuya = _tuya_device(self.hass, self._entry, self._device_id)
        if tuya is not None:
            tuya._pending_status = {}
            await tuya.async_release_tcp()
        self.schedule_update_ha_state()
        _poke(tuya)


class ScreenServerSelect(SelectEntity):
    """Which verified screen server this strip follows."""

    _attr_has_entity_name = True
    _attr_name = "Screen server"

    def __init__(self, entry, device_id, device):
        self._entry = entry
        self._device_id = device_id
        names = [
            server.get("name")
            for server in _servers(entry)
            if server.get("token") and server.get("verified")
        ]
        current = _sync(device).get("server")
        if current and current not in names:
            names.insert(0, current)
        if not names:
            names = ["No screen server yet"]
        self._attr_options = names
        self._attr_current_option = current if current in names else names[0]
        self._attr_unique_id = f"local_{device_id}_screen_server"
        self._attr_device_info = _device_info(device_id, device)

    async def async_select_option(self, option: str) -> None:
        if option == "No screen server yet":
            return
        device = self._entry.data[CONF_DEVICES][self._device_id]
        sync = _sync(device)
        sync["server"] = option
        sync.setdefault("zones", [])
        sync.setdefault("enabled", False)
        device["screen_sync"] = sync
        self._attr_current_option = option
        self.schedule_update_ha_state()
        _save(self.hass, self._entry)
        hub = _hubs(self.hass).get(self._entry.entry_id)
        if hub:
            await hub.restart_zones()


class ZoneLengthNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 400
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS

    def __init__(self, entry, device_id, device, index):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        zones = _zones(device)
        self._attr_native_value = int(zones[index].get("cm") or 0)
        self._attr_name = f"Zone {index + 1} length"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_cm"
        self._attr_device_info = _device_info(device_id, device)

    async def async_set_native_value(self, value: float) -> None:
        zones = _sync(self._entry.data[CONF_DEVICES][self._device_id]).setdefault("zones", [])
        zones[self._index]["cm"] = int(value)
        self._attr_native_value = int(value)
        self.schedule_update_ha_state()
        _save(self.hass, self._entry)


class ZoneEdgeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_options = list(EDGES)

    def __init__(self, entry, device_id, device, index):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        zones = _zones(device)
        self._attr_current_option = zones[index].get("edge") or "top"
        self._attr_name = f"Zone {index + 1} edge"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_edge"
        self._attr_device_info = _device_info(device_id, device)

    async def async_select_option(self, option: str) -> None:
        zones = _sync(self._entry.data[CONF_DEVICES][self._device_id]).setdefault("zones", [])
        zones[self._index]["edge"] = option
        self._attr_current_option = option
        self.schedule_update_ha_state()
        _save(self.hass, self._entry)


class AddZoneButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Add zone"

    def __init__(self, entry, device_id, device):
        self._entry = entry
        self._device_id = device_id
        self._attr_unique_id = f"local_{device_id}_add_zone"
        self._attr_device_info = _device_info(device_id, device)

    async def async_press(self) -> None:
        device = self._entry.data[CONF_DEVICES][self._device_id]
        sync = _sync(device)
        zones = list(sync.get("zones") or [])
        if len(zones) >= 4:
            return
        used = {zone.get("edge") for zone in zones}
        edge = next((name for name in EDGES if name not in used), "bottom")
        zones.append({"edge": edge, "cm": 20})
        sync["zones"] = zones
        device["screen_sync"] = sync
        await _persist_and_reload(self.hass, self._entry)


class RemoveZoneButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry, device_id, device, index):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        self._attr_name = f"Remove zone {index + 1}"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_remove"
        self._attr_device_info = _device_info(device_id, device)

    async def async_press(self) -> None:
        device = self._entry.data[CONF_DEVICES][self._device_id]
        sync = _sync(device)
        zones = list(sync.get("zones") or [])
        if self._index >= len(zones):
            return
        zones.pop(self._index)
        sync["zones"] = zones
        device["screen_sync"] = sync
        await _persist_and_reload(self.hass, self._entry)


def _strip_devices(entry):
    for device_id, device in entry.data.get(CONF_DEVICES, {}).items():
        yield device_id, device


def _drop_extra_zones(hass, entry):
    """Remove zone entities that are no longer part of a strip."""
    from homeassistant.helpers import entity_registry as er

    counts = {device_id: len(_zones(device)) for device_id, device in _strip_devices(entry)}
    registry = er.async_get(hass)
    for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        unique_id = entity.unique_id or ""
        marker = "_zone_"
        if marker not in unique_id or not unique_id.startswith("local_"):
            continue
        head, _, tail = unique_id.partition(marker)
        device_id = head[len("local_") :]
        index = tail.split("_", 1)[0]
        if device_id in counts and index.isdigit() and int(index) >= counts[device_id]:
            registry.async_remove(entity.entity_id)


async def async_add_switches(hass, entry, async_add_entities):
    _drop_extra_zones(hass, entry)
    async_add_entities(
        [ScreenSyncSwitch(entry, device_id, device) for device_id, device in _strip_devices(entry)]
    )


async def async_add_numbers(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _strip_devices(entry):
        for index in range(len(_zones(device))):
            entities.append(ZoneLengthNumber(entry, device_id, device, index))
    async_add_entities(entities)


async def async_add_selects(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _strip_devices(entry):
        entities.append(ScreenServerSelect(entry, device_id, device))
        for index in range(len(_zones(device))):
            entities.append(ZoneEdgeSelect(entry, device_id, device, index))
    async_add_entities(entities)


async def async_add_buttons(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _strip_devices(entry):
        zones = _zones(device)
        if len(zones) < 4:
            entities.append(AddZoneButton(entry, device_id, device))
        for index in range(len(zones)):
            entities.append(RemoveZoneButton(entry, device_id, device, index))
    async_add_entities(entities)


async def async_release_screen_sync(device):
    """Give the strip back to the light entity and reconnect local control."""
    entry = device._entry
    sync = _sync(entry.data[CONF_DEVICES][device.id])
    sync["enabled"] = False
    entry.data[CONF_DEVICES][device.id]["screen_sync"] = sync
    _save(device.hass, entry)
    hub = _hubs(device.hass).get(entry.entry_id)
    if hub:
        await hub.restart_zones()
    device._pending_status = {}
    await device.async_release_tcp()
    _poke(device)


def _hubs(hass):
    return hass.data[DOMAIN].setdefault("screen_hubs", {})


def screen_sync_enabled(entry, device_id):
    device = entry.data.get(CONF_DEVICES, {}).get(device_id) or {}
    return bool(_sync(device).get("enabled"))


async def async_start_hub(hass, entry):
    hub = ScreenHub(hass, entry)
    await hub.start()
    _hubs(hass)[entry.entry_id] = hub

    def _forget():
        _hubs(hass).pop(entry.entry_id, None)
        return hub.stop()

    entry.async_on_unload(_forget)
    return hub
