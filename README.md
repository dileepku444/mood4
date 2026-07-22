# Raylogic MOD2U - Home Assistant Integration

RE8-style config-flow architecture for the Raylogic MOD2U 2-channel universal
module (Relay / Dimmer / Fan / Curtain per channel).

## Setup

1. Copy `custom_components/raylogic_mod2u` into your HA `config/custom_components/`.
2. Restart Home Assistant.
3. Settings -> Devices & Services -> Add Integration -> "Raylogic MOD2U".
4. Fill in:
   - **IP Address** / **Port** (default 5550).
   - **Area** - the Area number shown on your device's Mod Settings screen
     (e.g. 12). Use `0` only if BOTH channels are Relay - HA will then learn
     the Area itself the first time you toggle each channel from the
     Raylogic GO app or a physical switch, instead of you typing it.
   - **Channel 1 Type** / **Channel 2 Type** - pick whatever you set on that
     channel in the Raylogic GO app's "Select Type" screen: `relay`,
     `dimmer`, `fan`, or `curtain`. The device does not report its own
     channel type over the network, so this has to be told to HA once.

## What's confirmed vs. not (from `Model_Number_Mod2u.txt` vendor capture)

| Type     | Status                                                        |
|----------|----------------------------------------------------------------|
| Relay    | Fully confirmed, both channels, any Area.                     |
| Fan      | Fully confirmed (off + 4 speeds), both channels, any Area.     |
| Dimmer   | Confirmed on/off + endpoints; brightness curve is a linear approximation between the two confirmed endpoints (0x01=full, 0xFF=off). |
| Curtain  | Confirmed for **Channel 1 only** (open/close/stop). Channel 2 curtain bytes were never captured. |
| CTC      | Not confirmed at all - no entity is created for this type yet. |

If you wire a curtain to Channel 2, or want a tighter dimmer curve, toggle
the control once from the Raylogic GO app while the HA log is on `debug`
for `custom_components.raylogic_mod2u`, and share the resulting `*AR=` line
so it can be added.

## Learn mode (Relay only, Area = 0)

If Area is left at `0` and a channel is Relay, HA passively listens for the
`*AR=` frame the module broadcasts when you toggle that channel from the
app or a physical switch, learns its Area, and creates the entity on the
fly (no restart needed). This does **not** work for Dimmer/Fan/Curtain -
those need Area set manually, since their "on" frame can't be reliably
told apart from a Relay frame just by listening.
