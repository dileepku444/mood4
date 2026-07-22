"""Raylogic MOD2U integration - RE8-style config-entry architecture."""
from __future__ import annotations
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_PORT, DOMAIN, PLATFORMS,
    LEGACY_DEFAULT_AREA, DEFAULT_CHANNEL_COUNT,
)
from .protocol import RaylogicMod2uDevice

_LOGGER = logging.getLogger(__name__)

CONF_LEGACY_AREA = "legacy_area"
CONF_LEGACY_CHANNEL_COUNT = "legacy_channel_count"
CONF_CHANNEL_START = "channel_start"
CONF_CH1_TYPE = "channel_1_type"
CONF_CH2_TYPE = "channel_2_type"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # options (naye "Configure" button se) data (initial add se) ke upar
    # priority lete hain - taaki channel type badalne ke baad delete+re-add
    # kiye bina bhi naya config turant effect kare.
    conf = {**entry.data, **entry.options}

    host = conf[CONF_HOST]
    port = conf.get(CONF_PORT, DEFAULT_PORT)
    # 0 = relay-only auto/learn mode - Area manually diya gaya nahi hai
    legacy_area = conf.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
    legacy_channel_count = conf.get(CONF_LEGACY_CHANNEL_COUNT, DEFAULT_CHANNEL_COUNT)
    # Kai installations mein channel numbering 1 se shuru nahi hoti (Area ke
    # andar globally assign hoti hai) - is device ka pehla channel number.
    channel_start = conf.get(CONF_CHANNEL_START, 1)
    # Raylogic GO app mein jo type set kiya gaya hai (relay/dimmer/fan/curtain)
    # - keys ab actual physical channel numbers hain (channel_start se shuru).
    channel_types = {
        channel_start: conf.get(CONF_CH1_TYPE, "relay"),
        channel_start + 1: conf.get(CONF_CH2_TYPE, "relay"),
    }

    device = RaylogicMod2uDevice(
        ip=host, port=port,
        legacy_area=legacy_area,
        legacy_channel_count=legacy_channel_count,
        channel_start=channel_start,
        channel_types=channel_types,
        state_callback=lambda ip, ch, state: _handle_state_update(
            hass, entry.entry_id, ip, ch, state
        ),
    )

    connected = await device.connect()
    if not connected:
        _LOGGER.error("Could not connect to Raylogic MOD2U at %s", host)
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = device

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options flow se save hote hi poora entry reload karo."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device: RaylogicMod2uDevice = hass.data[DOMAIN].get(entry.entry_id)
    if device:
        await device.disconnect()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _handle_state_update(hass, entry_id, ip, ch, state):
    if "available" in state:
        hass.bus.async_fire(
            f"{DOMAIN}_available",
            {"entry_id": entry_id, "available": state["available"]},
        )
        return
    hass.bus.async_fire(
        f"{DOMAIN}_state_update",
        {"entry_id": entry_id, "ip": ip, "channel": ch, "state": state},
    )
    hass.bus.async_fire(
        f"{DOMAIN}_available",
        {"entry_id": entry_id, "available": True},
    )
