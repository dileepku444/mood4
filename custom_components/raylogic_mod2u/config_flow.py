"""Config flow for Raylogic MOD2U integration."""
from __future__ import annotations
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DEFAULT_PORT, DOMAIN, AREA_MIN, AREA_MAX, LEGACY_DEFAULT_AREA, DEFAULT_CHANNEL_COUNT

_LOGGER = logging.getLogger(__name__)

CONF_LEGACY_AREA = "legacy_area"
CONF_LEGACY_CHANNEL_COUNT = "legacy_channel_count"
CONF_CHANNEL_START = "channel_start"
CONF_CH1_TYPE = "channel_1_type"
CONF_CH2_TYPE = "channel_2_type"

# MOD2U khud apna channel-type broadcast nahi karta (RE8 ke BR40 jaisa
# readback confirm nahi hua) - type sirf Raylogic GO app se set hota hai.
# Isliye auto-detect ki jagah, jo type aapne app mein (Select Type screen)
# choose kiya hai wahi yahan bata do - relay/dimmer/fan/curtain sab ke
# confirmed *AR= formats ab implement ho chuke hain.
CHANNEL_TYPE_OPTIONS = ["relay", "dimmer", "fan", "curtain"]

# NOTE: plain `int` (jaisa pehle tha) HA frontend mein kabhi-kabhi sirf
# spinner-arrows wala non-typeable box, ya (vol.Range ke saath) slider bana
# deta hai - directly type karna mushkil ho jaata hai (screenshot mein yehi
# dikh raha tha). NumberSelector(mode=BOX) explicitly ek plain, type-karne-
# yogya number field banata hai, har platform (web/mobile) par.
STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): selector.TextSelector(),
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
        )
    ),
    # 0 = LEARN mode (Relay-only): koi Area mat batao, HA khud pehle real
    # ON/OFF se Area+Channel seekh lega (app ya physical switch se ek baar
    # toggle karna hoga). Dimmer/Fan/Curtain ke liye LEARN nahi chalta -
    # unke liye Area yahin manually dena zaroori hai (jaisa "Area 12"
    # aapke Mod Settings screen mein dikhta hai).
    vol.Optional(CONF_LEGACY_AREA, default=LEGACY_DEFAULT_AREA): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX
        )
    ),
    vol.Optional(CONF_LEGACY_CHANNEL_COUNT, default=DEFAULT_CHANNEL_COUNT): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1, max=16, mode=selector.NumberSelectorMode.BOX
        )
    ),
    # Kai Raylogic installations mein Area ke andar channel numbers GLOBALLY
    # assign hote hain (har module 1 se shuru nahi hota) - agar Raylogic GO
    # app mein aapke is module ke channels "3, 4" jaise dikhte hain (1,2
    # nahi), to yahan 3 daal do taaki commands sahi channel number par jaayen.
    vol.Optional(CONF_CHANNEL_START, default=1): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1, max=255, mode=selector.NumberSelectorMode.BOX
        )
    ),
    vol.Optional(CONF_CH1_TYPE, default="relay"): selector.SelectSelector(
        selector.SelectSelectorConfig(options=CHANNEL_TYPE_OPTIONS)
    ),
    vol.Optional(CONF_CH2_TYPE, default="relay"): selector.SelectSelector(
        selector.SelectSelectorConfig(options=CHANNEL_TYPE_OPTIONS)
    ),
})


async def validate_connection(hass, host: str, port: int) -> dict:
    info: dict[str, Any] = {"mac": None, "node": None}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0,
        )
    except Exception as exc:
        raise ConnectionError(f"Cannot connect to {host}:{port}") from exc

    try:
        data = await asyncio.wait_for(reader.readuntil(b'\r'), timeout=5.0)
        line = data.decode(errors="replace").strip()
        if "*KA=" in line:
            info["node"] = line.split(",")[0].strip()
            info["mac"] = f"{host.replace('.', '_')}_{info['node']}"
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return info


class RaylogicMod2uConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return RaylogicMod2uOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # NumberSelector float lauta sakta hai (e.g. 5550.0) - int mein
            # cast karo taaki config entry aur protocol.py mein hamesha int
            # hi ho, jaisa pehle plain `int` schema se milta tha.
            user_input[CONF_PORT] = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            user_input[CONF_LEGACY_AREA] = int(
                user_input.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            )
            user_input[CONF_LEGACY_CHANNEL_COUNT] = int(
                user_input.get(CONF_LEGACY_CHANNEL_COUNT, DEFAULT_CHANNEL_COUNT)
            )
            user_input[CONF_CHANNEL_START] = int(user_input.get(CONF_CHANNEL_START, 1))
            ch_types = (
                user_input.get(CONF_CH1_TYPE, "relay"),
                user_input.get(CONF_CH2_TYPE, "relay"),
            )
            # LEARN mode (area=0) sirf Relay ke liye kaam karta hai - Dimmer/
            # Fan/Curtain ka *AR= echo se "type" pata nahi chal sakta, isliye
            # unke liye Area manually dena zaroori hai.
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                errors["base"] = "area_required_for_non_relay"
            else:
                host = user_input[CONF_HOST]
                port = user_input[CONF_PORT]
                try:
                    info = await validate_connection(self.hass, host, port)
                except ConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error connecting to %s", host)
                    errors["base"] = "unknown"
                else:
                    unique_id = info.get("mac") or host
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Raylogic MOD2U {host}",
                        data=user_input,
                    )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors,
        )


class RaylogicMod2uOptionsFlow(config_entries.OptionsFlow):
    """'Configure' button - taaki channel type (e.g. ch4 ko dimmer banana)
    ya Area/channel_start badalne ke liye device delete + dobara add na
    karna pade (jisse LEARN-mode ka saved state bhi udh jaata tha)."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            user_input[CONF_LEGACY_AREA] = int(
                user_input.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            )
            user_input[CONF_LEGACY_CHANNEL_COUNT] = int(
                user_input.get(CONF_LEGACY_CHANNEL_COUNT, DEFAULT_CHANNEL_COUNT)
            )
            user_input[CONF_CHANNEL_START] = int(user_input.get(CONF_CHANNEL_START, 1))
            ch_types = (
                user_input.get(CONF_CH1_TYPE, "relay"),
                user_input.get(CONF_CH2_TYPE, "relay"),
            )
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(current),
                    errors={"base": "area_required_for_non_relay"},
                )
            # HA reload karega automatically jab options update hote hain
            # (manifest mein config_flow: true ke saath), lekin safe rehne
            # ke liye khud bhi reload trigger karte hain.
            result = self.async_create_entry(title="", data=user_input)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return result

        return self.async_show_form(step_id="init", data_schema=self._schema(current))

    def _schema(self, current: dict) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): selector.TextSelector(),
            vol.Optional(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_LEGACY_AREA, default=current.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_LEGACY_CHANNEL_COUNT, default=current.get(CONF_LEGACY_CHANNEL_COUNT, DEFAULT_CHANNEL_COUNT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=16, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_CHANNEL_START, default=current.get(CONF_CHANNEL_START, 1)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=255, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_CH1_TYPE, default=current.get(CONF_CH1_TYPE, "relay")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=CHANNEL_TYPE_OPTIONS)
            ),
            vol.Optional(
                CONF_CH2_TYPE, default=current.get(CONF_CH2_TYPE, "relay")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=CHANNEL_TYPE_OPTIONS)
            ),
        })
