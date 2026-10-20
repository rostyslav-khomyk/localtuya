"""Follow a Hyperion screen-edge server and paint Battletron zone strips.

Several strips can share one server. Each strip has its own zone lengths.
Solid color, scenes, and music stay on the normal light entity. Turn this
switch off before using those.
"""

import asyncio
import logging

import aiohttp
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
        for server in _servers(self.entry):
            name = server["name"]
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
        if not cache:
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
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        await ws.send_json(
                            {
                                "command": "authorize",
                                "subcommand": "login",
                                "token": server["token"],
                                "tan": 1,
                            }
                        )
                        await ws.send_json(
                            {"command": "ledcolors", "subcommand": "ledstream-start", "tan": 2}
                        )
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
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Screen server %s: %s", server.get("name"), err)
                await asyncio.sleep(5)


def _device_info(device_id, device):
    name = device.get("friendly_name") or device.get("name") or device_id
    return DeviceInfo(identifiers={(DOMAIN, f"local_{device_id}")}, name=name)


class ScreenSyncSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Screen sync"

    def __init__(self, entry, device_id, device):
        self._entry = entry
        self._device_id = device_id
        self._attr_unique_id = f"local_{device_id}_screen_sync"
        self._attr_is_on = bool(_sync(device).get("enabled"))
        self._attr_device_info = _device_info(device_id, device)

    async def async_turn_on(self, **kwargs):
        _sync(self._entry.data[CONF_DEVICES][self._device_id])["enabled"] = True
        self._attr_is_on = True
        self.schedule_update_ha_state()
        await _persist_and_reload(self.hass, self._entry)

    async def async_turn_off(self, **kwargs):
        _sync(self._entry.data[CONF_DEVICES][self._device_id])["enabled"] = False
        self._attr_is_on = False
        self.schedule_update_ha_state()
        await _persist_and_reload(self.hass, self._entry)


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
        zones = _sync(device).get("zones") or []
        cm = int(zones[index]["cm"]) if index < len(zones) else 0
        self._attr_native_value = cm
        self._attr_name = f"Zone {index + 1} length"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_cm"
        self._attr_device_info = _device_info(device_id, device)

    async def async_set_native_value(self, value: float) -> None:
        zones = _sync(self._entry.data[CONF_DEVICES][self._device_id]).setdefault("zones", [])
        while len(zones) <= self._index:
            zones.append({"edge": "top", "cm": 0})
        zones[self._index]["cm"] = int(value)
        self._attr_native_value = int(value)
        self.schedule_update_ha_state()
        await _persist_and_reload(self.hass, self._entry)


class ZoneEdgeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_options = list(EDGES)

    def __init__(self, entry, device_id, device, index):
        self._entry = entry
        self._device_id = device_id
        self._index = index
        zones = _sync(device).get("zones") or []
        current = zones[index]["edge"] if index < len(zones) else "top"
        self._attr_current_option = current
        self._attr_name = f"Zone {index + 1} edge"
        self._attr_unique_id = f"local_{device_id}_zone_{index}_edge"
        self._attr_device_info = _device_info(device_id, device)

    async def async_select_option(self, option: str) -> None:
        zones = _sync(self._entry.data[CONF_DEVICES][self._device_id]).setdefault("zones", [])
        while len(zones) <= self._index:
            zones.append({"edge": "top", "cm": 0})
        zones[self._index]["edge"] = option
        self._attr_current_option = option
        self.schedule_update_ha_state()
        await _persist_and_reload(self.hass, self._entry)


async def _persist_and_reload(hass, entry):
    hass.config_entries.async_update_entry(entry, data=dict(entry.data))
    await hass.config_entries.async_reload(entry.entry_id)


def _synced_devices(entry):
    for device_id, device in entry.data.get(CONF_DEVICES, {}).items():
        if _sync(device):
            yield device_id, device


async def async_add_switches(hass, entry, async_add_entities):
    async_add_entities(
        [ScreenSyncSwitch(entry, device_id, device) for device_id, device in _synced_devices(entry)]
    )


async def async_add_numbers(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _synced_devices(entry):
        for index in range(4):
            entities.append(ZoneLengthNumber(entry, device_id, device, index))
    async_add_entities(entities)


async def async_add_selects(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _synced_devices(entry):
        for index in range(4):
            entities.append(ZoneEdgeSelect(entry, device_id, device, index))
    async_add_entities(entities)


async def async_start_hub(hass, entry):
    hub = ScreenHub(hass, entry)
    await hub.start()
    entry.async_on_unload(hub.stop)
    return hub
