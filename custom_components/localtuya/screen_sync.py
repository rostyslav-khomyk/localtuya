"""Zone layout entities for Battletron strips.

Zones are configured here (length + edge). Colour is controlled by the
standard zone ``light`` entities in ``zone_lights`` — not by Hyperion.
"""

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_DEVICES, UnitOfLength
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .screen_layout import EDGES

CONF_SCREEN_SYNC = "screen_sync"


def _sync(device):
    return device.get(CONF_SCREEN_SYNC) or {}


def _device_info(device_id, device):
    name = device.get("friendly_name") or device.get("name") or device_id
    return DeviceInfo(identifiers={(DOMAIN, f"local_{device_id}")}, name=name)


def _zones(device):
    return list(_sync(device).get("zones") or [])


def _save(hass, entry):
    """Persist zone layout without reloading the whole integration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    skips = domain_data.setdefault("_skip_reload_entries", set())
    skips.add(entry.entry_id)
    hass.config_entries.async_update_entry(entry, data=dict(entry.data))


async def _persist_and_reload(hass, entry):
    _save(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)


def _strip_devices(entry):
    for device_id, device in entry.data.get(CONF_DEVICES, {}).items():
        yield device_id, device


def _drop_extra_zones(hass, entry):
    """Remove stale zone / Hyperion screen-sync entities."""
    from homeassistant.helpers import entity_registry as er

    counts = {device_id: len(_zones(device)) for device_id, device in _strip_devices(entry)}
    registry = er.async_get(hass)
    for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        unique_id = entity.unique_id or ""
        if not unique_id.startswith("local_"):
            continue
        # Old Hyperion control surface — zone lights replaced these.
        if unique_id.endswith("_screen_sync") or unique_id.endswith("_screen_server"):
            registry.async_remove(entity.entity_id)
            continue
        marker = "_zone_"
        if marker not in unique_id:
            continue
        head, _, tail = unique_id.partition(marker)
        device_id = head[len("local_") :]
        index = tail.split("_", 1)[0]
        if device_id in counts and index.isdigit() and int(index) >= counts[device_id]:
            registry.async_remove(entity.entity_id)


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
        # Clear obsolete Hyperion flags.
        sync.pop("enabled", None)
        sync.pop("server", None)
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


async def async_add_numbers(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _strip_devices(entry):
        for index in range(len(_zones(device))):
            entities.append(ZoneLengthNumber(entry, device_id, device, index))
    async_add_entities(entities)


async def async_add_selects(hass, entry, async_add_entities):
    entities = []
    for device_id, device in _strip_devices(entry):
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


# Compatibility shims while light.py / coordinator migrate.
def screen_sync_enabled(entry, device_id):
    from .zone_lights import zones_painting

    hass = getattr(entry, "hass", None)
    # ConfigEntry has no hass; callers that need live state use zones_painting(hass,...).
    return False


async def async_release_screen_sync(device):
    from .zone_lights import async_release_zones

    await async_release_zones(device)


async def async_start_hub(hass, entry):
    from .zone_lights import async_start_zone_hub

    return await async_start_zone_hub(hass, entry)


async def async_add_switches(hass, entry, async_add_entities):
    """Screen sync switch removed — zone lights are the control surface."""
    async_add_entities([])
