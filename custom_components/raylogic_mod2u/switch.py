"""Raylogic MOD2U relay/switch platform.

Har channel jo abhi 'relay' type treat ho raha hai (auto-mode mein unknown
type-byte wale channels bhi safe default ke taur par yahin aate hain, jab
tak CHANNEL_TYPE_BYTE_MAP fill nahi hota) - wahi is platform mein switch
entity banta hai.
"""
from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CH_TYPE_RELAY, DEVICE_MODEL_NAME, DEVICE_MODEL_DESC
from .protocol import RaylogicMod2uDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicMod2uDevice = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RaylogicMod2uSwitch(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_RELAY
    ]
    if entities:
        _LOGGER.info("Setting up %d MOD2U relay channel(s) on %s", len(entities), device.ip)
        async_add_entities(entities)
    else:
        _LOGGER.info(
            "Raylogic MOD2U %s: koi channel abhi tak nahi seekha gaya "
            "(LEARN mode). Raylogic GO app ya physical switch se ek baar "
            "har channel ko ON/OFF karo - entity turant apne aap ban jayegi.",
            device.ip,
        )

    # LEARN mode mein naye channels runtime pe seekhe jaate hain - jab bhi
    # aisa hota hai, protocol.py is callback ko call karega taaki entity
    # DYNAMICALLY (HA restart kiye bina) add ho sake.
    def _on_new_channel(ch_num, state):
        async_add_entities([RaylogicMod2uSwitch(hass, entry, device, ch_num, state)])

    device.new_channel_callback = _on_new_channel


class RaylogicMod2uSwitch(SwitchEntity):
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicMod2uDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.node_id or device.ip}_mod2u_ch{ch_num}"
        self._attr_name = f"mod2u_{suffix}_area{area}_ch{ch_num}"
        self._is_on = initial_state.get("on", False)

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

    async def async_turn_on(self, **kwargs):
        await self._device.set_relay(self._ch_num, True)
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_relay(self._ch_num, False)
        self._is_on = False
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
                self.async_write_ha_state()

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()
