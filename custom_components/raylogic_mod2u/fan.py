"""Raylogic MOD2U fan platform.

Model_Number_Mod2u.txt capture se confirmed: 00 1A <area> <level> <channel>,
level 0x01=off, 0x02..0x05 = speed 1-4 (25/50/75/100%).
"""
from __future__ import annotations
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CH_TYPE_FAN, DEVICE_MODEL_NAME, DEVICE_MODEL_DESC
from .protocol import RaylogicMod2uDevice

_LOGGER = logging.getLogger(__name__)

SPEED_STEPS = [0, 25, 50, 75, 100]


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicMod2uDevice = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RaylogicMod2uFan(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_FAN
    ]
    if entities:
        _LOGGER.info("Setting up %d MOD2U fan channel(s) on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicMod2uFan(FanEntity):
    _attr_has_entity_name = False
    _attr_supported_features = FanEntityFeature.SET_SPEED
    _attr_speed_count = 4

    def __init__(self, hass, entry, device: RaylogicMod2uDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.node_id or device.ip}_mod2u_ch{ch_num}"
        self._attr_name = f"mod2u_{suffix}_area{area}_ch{ch_num}_fan"
        self._is_on = initial_state.get("on", False)
        self._percentage = initial_state.get("percentage", 0)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.node_id or self._device.ip)},
            name=f"Raylogic MOD2U ({self._device.ip})",
            manufacturer="Raylogic",
            model=f"{DEVICE_MODEL_NAME} - {DEVICE_MODEL_DESC}",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self):
        return self._device.is_connected

    @property
    def is_on(self):
        return self._is_on

    @property
    def percentage(self):
        return self._percentage

    async def async_set_percentage(self, percentage: int):
        step = min(SPEED_STEPS, key=lambda s: abs(s - percentage))
        await self._device.set_fan(self._ch_num, step)
        self._percentage = step
        self._is_on = step > 0
        self.async_write_ha_state()

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        await self.async_set_percentage(percentage or self._percentage or 100)

    async def async_turn_off(self, **kwargs):
        await self._device.set_fan(self._ch_num, 0)
        self._is_on = False
        self._percentage = 0
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_available", self._on_available)
        )

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._ch_num:
            s = d.get("state", {})
            if "on" in s:
                self._is_on = bool(s["on"])
            if "percentage" in s:
                self._percentage = s["percentage"]
            self.async_write_ha_state()

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()
