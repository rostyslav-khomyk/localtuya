"""HA master output switch, shared by manual zone lighting and screen sync."""

from homeassistant.components.switch import SwitchEntity

from .zone_lights import _hubs


class ScreenEdgesMasterSwitch(SwitchEntity):
    _attr_name = "ScreenEdges lighting"
    _attr_icon = "mdi:led-strip-variant"
    _attr_should_poll = False

    def __init__(self, entry):
        self._entry = entry
        self._attr_unique_id = f"local_{entry.entry_id}_screenedges_output"

    def _hub(self):
        return _hubs(self.hass)[self._entry.entry_id]

    @property
    def is_on(self):
        return self._hub().output_enabled

    async def async_turn_on(self, **kwargs):
        try:
            await self._hub().set_output_enabled(True)
        finally:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        try:
            await self._hub().set_output_enabled(False)
        finally:
            self.async_write_ha_state()
