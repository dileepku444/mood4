"""Constants for the Raylogic MOD2U integration.

MOD2U ek "universal module" hai (aapke screenshots ke Select Type screen se
confirmed): har channel ko app se Dimmer / Fan / Curtain / Relay / CTC /
Empty banaya ja sakta hai, aur poora device kisi bhi Area (1-16) mein add ho
sakta hai.

Is file mein 2 tarah ke values hain:
  1. CONFIRMED  - Docklight capture se pehle hi verify ho chuka (DILEEPGO
     repo se), sirf Relay ke liye.
  2. PLACEHOLDER / TODO - abhi tak koi capture available nahi hai. In values
     ko dummy na maano - jaha "TODO CAPTURE" likha hai, wahan real value
     pata chalte hi yahin update karna hai. Tab tak fallback logic (protocol.py
     mein) safe defaults use karta hai.
"""

DOMAIN = "raylogic_mod2u"

# Network -------------------------------------------------------------- #
DEFAULT_PORT = 5550
CONNECT_TIMEOUT = 5
RECONNECT_DELAY = 30

# ------------------------------------------------------------------ #
# BR40 identification
#
# H81/RE16/FN4/RE8 (Din-Re8 repo) sabka apna unique byte[1] BR40 code hai,
# jo *KA= push line ke andar byte[7] par milta hai (raylogic/protocol.py
# ka _extract_br40_code_from_ka dekho). MOD2U ka apna BR40 code ABHI TAK
# CONFIRM NAHI HUA - DILEEPGO repo mein koi BR40 query/response capture
# nahi tha, sirf direct *AR= relay commands the.
#
# TODO CAPTURE: Jab bhi MOD2U HA se connect ho, protocol.py raw *KA= line
# ko INFO level par log karega - us log line ka "byte[7]" (ya poora hex)
# humein bhejo, hum BR40_CODE_MOD2U yahan fill kar denge. Tab tak
# BR40_CODE_MOD2U = None rehta hai aur integration "legacy mode" (static
# 2-channel relay, jaisa DILEEPGO abhi karta hai) mein fallback karta hai.
# ------------------------------------------------------------------ #
BR40_CODE_MOD2U = None  # TODO CAPTURE - fill after first real *KA= log

DEVICE_MODEL_NAME = "MOD2U"
DEVICE_MODEL_DESC = "Universal 2-Channel Module (Dimmer/Fan/Curtain/Relay/CTC)"

# ------------------------------------------------------------------ #
# Area
# Screenshot mein "Area 12" dikhta hai aur DILEEPGO ka already-confirmed
# capture Area byte = 0x0C bhejta hai. 0x0C = 12 decimal -> ye match confirm
# karta hai ki Area byte = seedha area number ka hex hai. Isliye area
# ab hardcoded "0C" nahi, balki 1-16 ke beech koi bhi ho sakta hai.
# ------------------------------------------------------------------ #
AREA_MIN = 1
AREA_MAX = 16
LEGACY_DEFAULT_AREA = 0x0C  # 12 - sirf tab use hota hai jab BR40 na mile aur
                             # user ne config mein area na diya ho

# ------------------------------------------------------------------ #
# Command bytes - CONFIRMED (Docklight capture, DILEEPGO repo se)
#   <ID>,<Seq>,*AR=<AddrHigh:00><Cmd:1A><Area><Level><Channel>
#   Relay: Level 01=OFF, 02=ON
# ------------------------------------------------------------------ #
CMD_ADDR_HIGH = "00"
CMD_CHANNEL_DIRECT = "1A"
RELAY_LEVEL_ON = "02"
RELAY_LEVEL_OFF = "01"

# ------------------------------------------------------------------ #
# Dimmer - CONFIRMED (Model_Number_Mod2u.txt, vendor capture, Area 12):
#   Ch1 On :  *AR=001A0C0101  -> level=01 (full/on)
#   Ch1 Off:  *AR=001A0CFF01  -> level=FF (off)
#   Ch1 dimming ramp goes FF -> ... -> 69 as brightness increases
#   Ch2 same pattern with channel byte = 02
# Same frame shape as Relay (00 1A <area> <level> <channel>), only the
# level byte meaning is different: 0x01 = full brightness, 0xFF = off,
# values in between are a proprietary dim curve. No exact 256-step table
# was captured, so brightness is mapped linearly between the two
# confirmed endpoints (good enough approximation; refine if a fuller
# capture turns up).
# ------------------------------------------------------------------ #
DIMMER_LEVEL_ON = 0x01
DIMMER_LEVEL_OFF = 0xFF

# ------------------------------------------------------------------ #
# Fan - CONFIRMED (Model_Number_Mod2u.txt, Area 12):
#   Off:     *AR=001A0C0101 (ch1) / ...0102 (ch2) -> level=01
#   Speed 1: level=02   Speed 2: level=03
#   Speed 3: level=04   Speed 4: level=05
# Identical scheme to the Din-Re8 FN4 fan.
# ------------------------------------------------------------------ #
FAN_LEVEL_OFF = 0x01
FAN_SPEEDS = {0: 0x01, 25: 0x02, 50: 0x03, 75: 0x04, 100: 0x05}

# ------------------------------------------------------------------ #
# Curtain - CONFIRMED for Channel 1 only (Model_Number_Mod2u.txt, Area 12).
# Curtain uses a DIFFERENT frame shape than Relay/Dimmer/Fan (cmd byte
# 0x27/0x26 instead of 0x1A, and no visible per-channel byte in the
# captured samples) - so these are stored as exact literal command
# strings rather than derived from a formula. Channel 2 curtain bytes
# were NOT captured - if you wire a curtain to channel 2, toggle it once
# from the Raylogic GO app while watching the HA log
# (raylogic_mod2u debug log) and share the *AR= line so it can be added.
# ------------------------------------------------------------------ #
CURTAIN_CH1_OPEN = "0027010105"
CURTAIN_CH1_CLOSE = "0027010205"
CURTAIN_CH1_STOP = "0026010000"

# ------------------------------------------------------------------ #
# +AR40= channel-type map - CONFIRMED (Model_Number_Mod2u.txt).
# This is a CONFIGURATION frame the Raylogic GO app sends to the module
# to SET each channel's mode (relay/dimmer/fan/curtain) - it is not a
# readback/query the module answers on its own, so it can't be used for
# live auto-detection the way RE8's +BR40= can. It's kept here for
# reference/documentation and for a future "set channel mode from HA"
# service, and to interpret the byte if it's ever seen echoed back.
#   Bytes (12 total, after +AR40=): 01 01 <ch_count> <ch1_type> <ch1_sub>
#   <ch2_type> <ch2_sub> 00 00 00 FF FF
#   ch_type: 00=relay 01=dimmer 02=fan 03=curtain
# ------------------------------------------------------------------ #
AR40_TYPE_RELAY = 0x00
AR40_TYPE_DIMMER = 0x01
AR40_TYPE_FAN = 0x02
AR40_TYPE_CURTAIN = 0x03

# ------------------------------------------------------------------ #
# Channel types (per-channel, as seen in "Select Type" screen)
# Byte-level encoding for these is NOT confirmed for MOD2U yet. Jab tak
# BR40 record parsing MOD2U ke liye nahi milta, har channel "relay" hi
# treated hota hai (safe/known-good), aur raw record bytes log hote hain
# taaki future mein CHANNEL_TYPE_BYTE_MAP bhara ja sake.
# ------------------------------------------------------------------ #
CH_TYPE_DIMMER = "dimmer"
CH_TYPE_FAN = "fan"
CH_TYPE_CURTAIN = "curtain"
CH_TYPE_RELAY = "relay"
CH_TYPE_CTC = "ctc"
CH_TYPE_EMPTY = "empty"

# TODO CAPTURE: <raw type byte value in BR40 record> -> CH_TYPE_*
CHANNEL_TYPE_BYTE_MAP: dict[int, str] = {
    # 0x02: CH_TYPE_RELAY,   # example - fill in once confirmed
}

DEFAULT_CHANNEL_COUNT = 2  # legacy MOD2U default (matches DILEEPGO)

PLATFORMS = ["switch", "light", "fan", "cover"]

KEEPALIVE_CMD = "*KA=01"

# ------------------------------------------------------------------ #
# Client sender-ID - CONFIRMED from real Docklight capture (device
# 192.168.1.34:5550, connected DIRECTLY, no TCP-HUB machine in between).
# Real traffic shows TWO different identities on the wire:
#   "109,...,*KA=..." / "109,...,+AR40=..." -> the MODULE/HUB's OWN
#     identity broadcasting its status. NOT to be reused as our sender id
#     (device ignores/loops commands that claim to be from itself).
#   "099,155,*AR=001A040203" (and 099,158 / 099,159 / 099,160...) -> a
#     CLIENT session's real, working *AR= commands (mobile app's own
#     session sending real accepted commands). This is genuinely honored
#     by the device, so we mirror it for our own outgoing commands.
# Official PDF's bare "*AR=...\r" (no prefix) examples are the LOGICAL
# payload only - real wire traffic always carries this <id>,<seq>, prefix.
# ------------------------------------------------------------------ #
CLIENT_SENDER_ID = "099"
KEEPALIVE_INTERVAL = 5

# ------------------------------------------------------------------ #
# Periodic resync (soft-reconnect)
#
# Confirmed via real-world test: device apna CORRECT, up-to-date state
# sirf ek NAYE connection ke initial burst par deta hai (isi wajah se
# Raylogic App band-khol karne par sahi status dikhata hai, chahe HA se
# change kiya ho). Device kisi bhi channel (Relay ho ya Dimmer) ka state
# change doosre already-connected sessions ko live broadcast NAHI karta.
#
# Isliye HA yahan periodically apna connection khud band-khol karta hai
# (background mein, entities/commands mein koi rukawat nahi) - bilkul
# App reopen karne jaisa hi effect - taaki dono taraf (App se kiya gaya
# change HA mein, aur HA se kiya gaya change App mein) kuch hi second mein
# sync ho jaaye, bina live-push ke bharose rahe.
# ------------------------------------------------------------------ #
RESYNC_INTERVAL = 45
