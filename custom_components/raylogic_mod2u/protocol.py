"""Raylogic MOD2U TCP protocol client - RE8-style architecture.

Do modes mein kaam karta hai:

1. AUTO / BR40 mode (RE8 jaisa):
   Device se `?BR40=` query karke har channel ka Area (ch_index) aur
   (jab byte map fill ho jaye) uska configured Type padhta hai. Isse
   device ko *kisi bhi* Area (1-16) mein add karo, integration khud
   detect kar leta hai - manual config nahi chahiye.
   Ye tab hi chalega jab BR40_CODE_MOD2U (const.py) discover ho chuka ho.

2. LEGACY / static mode (fallback, jab tak BR40 code na mile):
   DILEEPGO ke original behaviour jaisa - fixed channel count (default 2),
   area config se ya LEGACY_DEFAULT_AREA (0x0C) se liya jata hai. Relay
   control fully working rehta hai is mode mein bhi (kyunki Relay ka
   command format already confirmed hai) - sirf auto-discovery nahi hoti.

Jaise hi BR40_CODE_MOD2U aur CHANNEL_TYPE_BYTE_MAP const.py mein bhar diye
jayenge, integration khud AUTO mode mein switch ho jayega - is file mein
koi aur change nahi karna padega.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .const import (
    CONNECT_TIMEOUT,
    CMD_ADDR_HIGH,
    CMD_CHANNEL_DIRECT,
    RELAY_LEVEL_ON,
    RELAY_LEVEL_OFF,
    DIMMER_LEVEL_ON,
    DIMMER_LEVEL_OFF,
    FAN_LEVEL_OFF,
    FAN_SPEEDS,
    CURTAIN_CH1_OPEN,
    CURTAIN_CH1_CLOSE,
    CURTAIN_CH1_STOP,
    BR40_CODE_MOD2U,
    CLIENT_SENDER_ID,
    RESYNC_INTERVAL,
    CHANNEL_TYPE_BYTE_MAP,
    CH_TYPE_RELAY,
    CH_TYPE_DIMMER,
    CH_TYPE_FAN,
    CH_TYPE_CURTAIN,
    DEFAULT_CHANNEL_COUNT,
    LEGACY_DEFAULT_AREA,
    AREA_MIN,
    AREA_MAX,
    KEEPALIVE_CMD,
    KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).parent / "device_state"
_STATE_DIR.mkdir(exist_ok=True)


class RaylogicMod2uDevice:
    """Ek physical MOD2U module = ek TCP connection."""

    def __init__(
        self,
        ip: str,
        port: int,
        legacy_area: int = LEGACY_DEFAULT_AREA,
        legacy_channel_count: int = DEFAULT_CHANNEL_COUNT,
        channel_start: int = 1,
        channel_types: Optional[dict[int, str]] = None,
        state_callback: Optional[Callable] = None,
    ):
        self.ip = ip
        self.port = port
        self.state_callback = state_callback

        # Legacy-mode fallback settings (config_flow se ya defaults se)
        self._legacy_area = legacy_area
        self._legacy_channel_count = legacy_channel_count
        # Kai installations mein is module ka pehla physical channel number
        # 1 nahi hota (Area ke andar globally assign hota hai) - jaise
        # confirm hua ek real capture mein: manually-created "ch1/ch2" kaam
        # nahi kar rahe the, kyunki us device ke asli channels 3,4 the.
        self._channel_start = max(1, channel_start)
        # Raylogic GO app mein har channel ka jo type set kiya gaya hai
        # (relay/dimmer/fan/curtain) - device khud ye batata nahi hai, isliye
        # config_flow se manually aata hai. {ch_num: type_str}
        self._channel_types: dict[int, str] = channel_types or {}

        # LEARN mode: koi manual area/channel count na diya ho to device
        # khud *AR= echo (app/physical switch se) sunkar Area + Channel
        # seekhta hai - DIN devices ke BR40 auto-detect jaisa hi result,
        # bina kisi unknown byte guess kiye (sirf confirmed Relay format
        # use hota hai: 00 1A <area> <level> <channel>).
        self._state_key = f"{ip.replace('.', '_')}_{port}"
        self._state_file = _STATE_DIR / f"{self._state_key}_learned.json"
        self._learned: dict[int, int] = self._load_learned()  # {ch_num: area}

        # switch.py registers this - called with (ch_num, initial_state)
        # jab bhi koi NAYA channel pehli baar seekha jaaye, taaki entity
        # turant HA mein dynamically add ho sake.
        self.new_channel_callback: Optional[Callable] = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._msg_counter = 0
        self._listen_task: Optional[asyncio.Task] = None
        self._ka_task: Optional[asyncio.Task] = None
        self._resync_task: Optional[asyncio.Task] = None
        self._resyncing = False  # soft-reconnect ke dauran duplicate na ho
        # BUG FIX: pehle read-error, write-error, aur periodic-resync teeno
        # apna-apna independent reconnect/connect() chala sakte the - agar
        # ek hi time par 2 chal jaate (jaisa real disconnect + resync ka
        # coincide hona), device par EK SAATH 2 TCP connections khul jaate
        # the. Ye chhota embedded device isse confuse ho kar atak jaata tha
        # - HA integration reload karna padta tha. Ab connect() sirf is
        # lock ke andar hi chalta hai (ek time par ek hi attempt), aur
        # _reconnecting flag duplicate delayed-retry schedule hone se rokta
        # hai.
        self._connect_lock = asyncio.Lock()
        self._reconnecting = False

        # Device identity
        self.node_id: Optional[str] = None       # e.g. "101" - device ka apna ID
        self.mac: Optional[str] = None
        self.fw_version: Optional[str] = None
        self.br40_code: Optional[int] = None
        self.auto_mode: bool = False              # True jab BR40 se channels mil jayein

        # channel_states[ch_num] = {"area": int, "type": str, "on": bool, ...}
        self.channel_states: dict[int, dict] = {}

    # ------------------------------------------------------------------ #
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def ip_suffix(self) -> str:
        return self.ip.split(".")[-1]

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        async with self._connect_lock:
            if self._connected:
                _LOGGER.debug(
                    "Raylogic MOD2U %s: already connected, skip duplicate "
                    "connect() call.", self.ip,
                )
                return True
            return await self._do_connect()

    async def _do_connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=float(CONNECT_TIMEOUT),
            )
            self._connected = True
            self._reconnecting = False
            _LOGGER.info("Connected to Raylogic MOD2U at %s", self.ip)

            await self._drain_initial_push()

            if self.br40_code is None:
                for _ in range(3):
                    line = await self._read_line(timeout=2.0)
                    if line and "*KA=" in line:
                        self._handle_ka_line(line)
                        break

            await self._try_auto_discovery()

            if not self.auto_mode:
                _LOGGER.warning(
                    "Raylogic MOD2U %s: BR40 auto-discovery nahi hui "
                    "(BR40_CODE_MOD2U abhi None hai ya device ne jawab nahi diya). "
                    "LEGACY mode mein chal raha hai: area=0x%02X, channels=%d. "
                    "Yeh sab Relay control ke liye kaam karega. Auto-detect "
                    "enable karne ke liye upar wali *KA= log line share karo.",
                    self.ip, self._legacy_area, self._legacy_channel_count,
                )
                self._setup_legacy_channels()

            self._listen_task = asyncio.create_task(self._listen_loop())
            self._ka_task = asyncio.create_task(self._keepalive_loop())
            self._resync_task = asyncio.create_task(self._resync_loop())

            if self.state_callback:
                self.state_callback(self.ip, None, {"available": True})

            return True

        except Exception as exc:
            _LOGGER.error("Failed to connect to Raylogic MOD2U %s: %s", self.ip, exc)
            self._connected = False
            return False

    async def disconnect(self):
        self._connected = False
        for task in (self._listen_task, self._ka_task, self._resync_task):
            if task:
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _reconnect(self):
        if self._reconnecting:
            _LOGGER.debug(
                "Raylogic MOD2U %s: reconnect already scheduled/in-progress "
                "- skipping duplicate.", self.ip,
            )
            return
        self._reconnecting = True
        try:
            _LOGGER.warning("Connection lost to MOD2U %s, retrying in 30s", self.ip)
            if self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
            await asyncio.sleep(30)
            await self.connect()
        finally:
            self._reconnecting = False

    def _schedule_reconnect(self):
        """Read-error, write-error, ya resync-fail - kahin se bhi reconnect
        chahiye ho, hamesha isi se guzro - taaki ek time par sirf EK
        reconnect chale (device par 2 TCP connections ek saath khulne se
        device khud confuse ho kar atak jaata tha, HA reload karna padta
        tha)."""
        if not self._reconnecting:
            asyncio.create_task(self._reconnect())

    async def _resync_loop(self):
        """Har RESYNC_INTERVAL second mein connection ko khud band-khol
        karta hai - App reopen karne jaisa hi effect, taaki Raylogic App se
        kiya gaya koi bhi change (jo live-broadcast nahi hota) kuch second
        mein HA mein bhi reflect ho jaaye. HA se bheji gayi commands (jo
        instantly optimistically apply hoti hain) is se disturb nahi hoti."""
        while self._connected and not self._resyncing:
            await asyncio.sleep(RESYNC_INTERVAL)
            if not self._connected or self._resyncing:
                return
            _LOGGER.debug(
                "Raylogic MOD2U %s: periodic resync (App-reopen jaisa "
                "soft-reconnect) taaki App se hue changes bhi sync ho jaayein.",
                self.ip,
            )
            await self._soft_reconnect()
            return  # naya connect() apna khud ka fresh resync-loop shuru kar dega

    async def _soft_reconnect(self):
        """Purana socket band karke turant naya connect() - is baar
        'available: False' event fire NAHI karte (bahut chhota gap hota
        hai, HA UI mein flicker nahi dikhna chahiye jab tak reconnect
        sach mein fail na ho jaaye)."""
        self._resyncing = True
        for task in (self._listen_task, self._ka_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        self._resyncing = False
        ok = await self.connect()
        if not ok:
            _LOGGER.warning(
                "Raylogic MOD2U %s: periodic resync fail hua, normal 30s "
                "reconnect cycle sambhal lega.", self.ip,
            )
            if self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
            self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #
    def _next_msg(self) -> str:
        self._msg_counter = (self._msg_counter % 999) + 1
        return f"{self._msg_counter:03d}"

    async def _send_raw(self, cmd: str):
        if not self._connected or not self._writer:
            _LOGGER.warning(
                "Raylogic MOD2U %s: command DROP hui kyunki connection abhi "
                "active nahi hai (connected=%s) - '%s' bheja nahi ja saka. "
                "Device se connection wapas ban raha hoga (30s reconnect "
                "cycle) - thodi der baad dobara try karo.",
                self.ip, self._connected, cmd,
            )
            return
        try:
            self._writer.write((cmd + "\r").encode())
            await self._writer.drain()
            _LOGGER.debug("TX %s: %s", self.ip, cmd)
        except Exception as exc:
            _LOGGER.error("Send error to MOD2U %s: %s", self.ip, exc)
            self._connected = False
            self._schedule_reconnect()

    async def _send_addressed(self, cmd: str):
        """CONFIRMED from real Docklight capture (device connected DIRECTLY,
        192.168.1.34:5550): wire traffic ALWAYS carries a "<id>,<seq>,"
        prefix before *AR=/+AR40= - the official PDF's bare "*AR=...\\r"
        examples are only the logical payload, not the real wire format.

        Pehle do galtiyan hui thi:
          1. Prefix bilkul hata diya tha (PDF examples dekh kar) - galat,
             real traffic mein prefix hota hai.
          2. Device ke apne broadcast id (jo *KA=/+AR40= lines mein "109"
             jaisa dikhta hai) ko apna sender-id samajh liya tha - galat,
             wo device/hub ki APNI identity hai, hamari nahi. Real working
             client commands (jaise "099,155,*AR=001A040203") ek ALAG id
             use karte hain - wahi CLIENT_SENDER_ID hai.
        """
        await self._send_raw(f"{CLIENT_SENDER_ID},{self._next_msg()},{cmd}")

    async def _read_line(self, timeout: float = 2.0) -> Optional[str]:
        try:
            data = await asyncio.wait_for(self._reader.readuntil(b'\r'), timeout=timeout)
            return data.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError as exc:
            line = exc.partial.decode(errors="replace").strip()
            return line if line else None
        except Exception as exc:
            _LOGGER.error("Read error from MOD2U %s: %s", self.ip, exc)
            self._connected = False
            self._schedule_reconnect()
            return None

    async def _drain_initial_push(self):
        """Naye connection banate hi device jo bhi initial burst bhejta hai
        (App connect karte waqt bhi yahi hota hoga, isiliye reopen karne par
        App ko sahi status milta hai) - pehle hum sirf PEHLI line padh kar
        baaki discard kar dete the. Ab thodi der (2.5s) tak jitni bhi lines
        aayein, sabko _dispatch_line se process karte hain - agar isme
        per-channel *AR= state bhi ho, wo ab channel_states mein reflect
        hogi (state_callback bhi fire hoga, taaki HA entities turant update
        ho jayein)."""
        end_time = asyncio.get_event_loop().time() + 2.5
        first = True
        while asyncio.get_event_loop().time() < end_time:
            remaining = max(0.1, end_time - asyncio.get_event_loop().time())
            line = await self._read_line(timeout=remaining)
            if not line:
                break
            if first and "*KA=" in line:
                self._handle_ka_line(line)
            else:
                self._dispatch_line(line)
            first = False

    def _handle_ka_line(self, line: str):
        """*KA= line device/hub KHUD apni identity broadcast karne ke liye
        bhejta hai (e.g. "109,*KA=31-...") - ye HAMARA sender-id NAHI hai,
        sirf reference/logging ke liye store karte hain. Outgoing commands
        CLIENT_SENDER_ID (confirmed "099") use karte hain."""
        try:
            candidate = line.split(",")[0].strip()
            if candidate.isdigit():
                self.node_id = candidate
        except Exception:
            pass

        # TODO CAPTURE: yahi wo jagah hai jaha H81/RE16/FN4/RE8 apna BR40
        # code nikalte hain (data_bytes[7]). Raw hex hamesha log karte hain
        # taaki jab MOD2U ka capture milega, hum sahi offset confirm kar sakein.
        try:
            if "*KA=" in line and "-" in line:
                ka_data = line.split("*KA=")[1]
                _, hex_data = ka_data.split("-")
                data_bytes = bytes.fromhex(hex_data.strip())
                _LOGGER.info(
                    "Raylogic MOD2U %s raw *KA= bytes (share this for BR40 "
                    "auto-detect setup): %s", self.ip, data_bytes.hex().upper(),
                )
                if BR40_CODE_MOD2U is not None and len(data_bytes) > 7:
                    self.br40_code = data_bytes[7]
        except Exception as exc:
            _LOGGER.debug("MOD2U KA parse note (not fatal, capture-only): %s", exc)

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def _try_auto_discovery(self):
        """Agar BR40 code known hai (const.py filled), device se channel
        list (Area + Type per channel) query karo. Warna silently skip."""
        if BR40_CODE_MOD2U is None:
            return
        await asyncio.sleep(0.5)
        code_hex = f"01{BR40_CODE_MOD2U:02X}"
        await self._send_addressed(f"?BR40={code_hex}")
        for _ in range(6):
            line = await self._read_line(timeout=3.0)
            if not line:
                continue
            if "+BR40=" in line:
                if self._parse_br40(line):
                    self.auto_mode = True
                    _LOGGER.info(
                        "Raylogic MOD2U %s: auto-discovered %d channels",
                        self.ip, len(self.channel_states),
                    )
                return
            if "*KA=" in line:
                await self._send_raw("*KA=2")

    def _parse_br40(self, line: str) -> bool:
        """TODO CAPTURE: record layout (size, ch_index offset, type-byte
        offset) abhi H81/FN4 jaisa assume kiya hai (8-byte record, byte[0]
        = area/ch_index). Confirm hone tak in numbers ko capture se verify
        karo."""
        try:
            data_hex = line.split("+BR40=")[1].strip()
            data = bytes.fromhex(data_hex)
            if len(data) < 3:
                return False
            ch_count = data[2]
            records = data[3:]
            record_size = 8
            if len(records) < ch_count * record_size:
                _LOGGER.warning(
                    "Raylogic MOD2U %s: BR40 response too short for assumed "
                    "record size - raw hex: %s", self.ip, data_hex,
                )
                return False

            for i in range(ch_count):
                r = records[i * record_size:(i + 1) * record_size]
                ch_num = i + 1
                area = r[0]
                type_byte = r[1] if len(r) > 1 else None
                ch_type = CHANNEL_TYPE_BYTE_MAP.get(type_byte, CH_TYPE_RELAY)
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": ch_type,
                    "raw_type_byte": type_byte,
                    "on": False,
                }
                _LOGGER.info(
                    "Raylogic MOD2U %s ch%d: area=%d type_byte=0x%02X -> "
                    "treated as '%s' (update CHANNEL_TYPE_BYTE_MAP once "
                    "confirmed in app)",
                    self.ip, ch_num, area, type_byte or 0, ch_type,
                )
            return True
        except Exception as exc:
            _LOGGER.warning("MOD2U BR40 parse error '%s': %s", line, exc)
            return False

    def _setup_legacy_channels(self):
        """Agar user ne config_flow mein manually Area diya hai (0 ka matlab
        'auto/learn', sirf Relay ke liye), turant channels bana do - har
        channel ka type wahi jo config mein select kiya gaya hai (relay/
        dimmer/fan/curtain). Warna (LEARN mode) kuch bhi nahi banata jab tak
        real *AR= frame na aa jaye (app/switch se ek baar toggle karna hoga)
        - LEARN sirf Relay channels ke liye kaam karta hai.

        NOTE: ye function periodic resync (_soft_reconnect) ke baad bhi
        chalta hai - isliye agar channel PEHLE se maujood hai (purani
        session se), uski on/brightness/percentage state ko as-is rehne do,
        sirf area/type refresh karo. Warna har resync par HA mein light/
        switch galti se OFF flicker karti (jabki device asal mein badla
        nahi tha - naya connect() ke baad turant _drain_initial_push jo
        fresh *AR= bheje wahi asli update dega)."""
        if self._legacy_area and self._legacy_area > 0:
            area = max(AREA_MIN, min(AREA_MAX, self._legacy_area))
            start = self._channel_start
            for ch_num in range(start, start + self._legacy_channel_count):
                ch_type = self._channel_types.get(ch_num, CH_TYPE_RELAY)
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": ch_type,
                    "on": existing.get("on", False),
                    "brightness": existing.get("brightness", 0),
                    "percentage": existing.get("percentage", 0),
                    "learned": False,
                }
            _LOGGER.info(
                "Raylogic MOD2U %s: manual mode - area=%d, channels %d-%d "
                "ready (types: %s).", self.ip, area, start,
                start + self._legacy_channel_count - 1,
                {k: v.get("type") for k, v in self.channel_states.items()},
            )
        else:
            # LEARN mode: pichhle session mein seekhe hue Relay channels
            # turant restore kar do (disk se), naye channels *AR= frame se
            # aayenge. Sirf relay type yahan chalta hai.
            for ch_num, area in self._learned.items():
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": CH_TYPE_RELAY,
                    "on": existing.get("on", False),
                    "learned": True,
                }
            _LOGGER.info(
                "Raylogic MOD2U %s: LEARN mode - %d channel(s) restored from "
                "previous session. Naye channel ke liye Raylogic GO app ya "
                "physical switch se ek baar us channel ko ON/OFF karo - HA "
                "khud detect karke entity bana dega.",
                self.ip, len(self._learned),
            )

    # ------------------------------------------------------------------ #
    # Learned-channel persistence
    # ------------------------------------------------------------------ #
    def _load_learned(self) -> dict[int, int]:
        try:
            data = json.loads(self._state_file.read_text())
            return {int(k): int(v) for k, v in data.items()}
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}

    def _save_learned(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps({str(k): v for k, v in self._learned.items()})
            )
        except OSError as err:
            _LOGGER.warning("Raylogic MOD2U %s: learned-state save fail: %s", self.ip, err)

    def _learn_channel(self, ch_num: int, area: int) -> bool:
        """Naya channel record karo. True return karta hai agar ye pehli
        baar dekha gaya channel tha (matlab entity create karni chahiye)."""
        is_new = ch_num not in self.channel_states
        if is_new or self.channel_states[ch_num].get("area") != area:
            self.channel_states[ch_num] = {
                "area": area, "type": CH_TYPE_RELAY, "on": False, "learned": True,
            }
            self._learned[ch_num] = area
            self._save_learned()
            _LOGGER.info(
                "Raylogic MOD2U %s: LEARNED new channel %d in area %d "
                "(from a real *AR= frame).", self.ip, ch_num, area,
            )
        return is_new

    # ------------------------------------------------------------------ #
    # Control - Relay (CONFIRMED format, works in both auto & legacy mode)
    # ------------------------------------------------------------------ #
    async def set_relay(self, ch_num: int, on: bool):
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic MOD2U %s: channel %d ka Area abhi maloom nahi hai "
                "(LEARN mode mein ho aur ye channel abhi tak seekha nahi gaya) "
                "- command bheja nahi ja sakta. Pehle Raylogic GO app ya "
                "physical switch se is channel ko ek baar ON/OFF karo.",
                self.ip, ch_num,
            )
            return
        level = RELAY_LEVEL_ON if on else RELAY_LEVEL_OFF
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update({"on": on})
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Dimmer (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01 = full brightness, 0xFF = off, in-between = dim curve.
    #   Linear approximation: brightness (0-255) -> level.
    # ------------------------------------------------------------------ #
    async def set_dimmer(self, ch_num: int, brightness: Optional[int]):
        """brightness: 0-255 (HA scale), ya None/0 = off."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic MOD2U %s: channel %d ka Area maloom nahi hai - "
                "dimmer command bheja nahi ja sakta.", self.ip, ch_num,
            )
            return
        if not brightness:
            level = DIMMER_LEVEL_OFF
        else:
            brightness = max(1, min(255, brightness))
            # 255 (full) -> level 0x01, dim karte hue level badhta jaata hai.
            # 254 tak hi jaane do - 255 (0xFF) sirf OFF ke liye reserved hai,
            # warna sabse dim "on" brightness galti se OFF command ban jaata.
            level = max(DIMMER_LEVEL_ON, min(254, 256 - brightness))
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": bool(brightness), "brightness": brightness or 0}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Fan (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01=off, 0x02=speed1(25%), 0x03=speed2(50%),
    #          0x04=speed3(75%), 0x05=speed4/full(100%)
    # ------------------------------------------------------------------ #
    async def set_fan(self, ch_num: int, percentage: int):
        """percentage: 0, 25, 50, 75, 100 (HA fan speed steps)."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic MOD2U %s: channel %d ka Area maloom nahi hai - "
                "fan command bheja nahi ja sakta.", self.ip, ch_num,
            )
            return
        # Nearest confirmed step le lo (0/25/50/75/100)
        step = min(FAN_SPEEDS.keys(), key=lambda k: abs(k - percentage))
        level = FAN_SPEEDS[step]
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": step > 0, "percentage": step}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Curtain (CONFIRMED for Channel 1 ONLY, literal commands -
    # curtain uses a different frame shape than relay/dimmer/fan, no
    # channel-derivable formula was captured yet).
    # ------------------------------------------------------------------ #
    async def set_cover(self, ch_num: int, action: str):
        """action: 'open' | 'close' | 'stop'."""
        if ch_num != 1:
            _LOGGER.warning(
                "Raylogic MOD2U %s: curtain bytes sirf Channel 1 ke liye "
                "confirmed hain. Channel %d curtain ke liye Raylogic GO app "
                "se ek baar open/close/stop karke us *AR= line ko log se "
                "share karo.", self.ip, ch_num,
            )
            return
        cmd_map = {
            "open": CURTAIN_CH1_OPEN,
            "close": CURTAIN_CH1_CLOSE,
            "stop": CURTAIN_CH1_STOP,
        }
        cmd_hex = cmd_map.get(action)
        if not cmd_hex:
            return
        await self._send_addressed(f"*AR={cmd_hex}")
        if action != "stop":
            self.channel_states.setdefault(ch_num, {}).update(
                {"on": action == "open", "moving": True}
            )
            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #
    async def _listen_loop(self):
        while self._connected:
            line = await self._read_line(timeout=30.0)
            if line:
                self._dispatch_line(line)

    async def _keepalive_loop(self):
        while self._connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._connected:
                await self._send_raw(KEEPALIVE_CMD)

    def _dispatch_line(self, line: str):
        if "*KA=" in line:
            self._handle_ka_line(line)
        elif "+BR40=" in line:
            self._parse_br40(line)
        elif "*AR=" in line:
            self._handle_ar(line)

    def _decode_level(self, ch_type: str, level: int) -> dict:
        """Incoming *AR= level byte ko channel-TYPE ke hisaab se decode karo.
        Pehle ye hamesha Relay ka check (level==0x02) use karta tha - isliye
        Dimmer/Fan channels ka status (aur Dimmer ki brightness) kabhi sahi
        update hi nahi hota tha, chahe device se sahi frame aa raha ho."""
        if ch_type == CH_TYPE_DIMMER:
            if level == DIMMER_LEVEL_OFF:
                return {"on": False, "brightness": 0}
            brightness = max(1, min(255, 256 - level))
            return {"on": True, "brightness": brightness}
        if ch_type == CH_TYPE_FAN:
            if level == FAN_LEVEL_OFF:
                return {"on": False, "percentage": 0}
            step = next(
                (pct for pct, lvl in FAN_SPEEDS.items() if lvl == level), None
            )
            if step is None:
                step = min(FAN_SPEEDS, key=lambda p: abs(FAN_SPEEDS[p] - level))
            return {"on": step > 0, "percentage": step}
        # Relay (default)
        return {"on": f"{level:02X}" == RELAY_LEVEL_ON}

    def _handle_ar(self, line: str):
        """Mobile app ya kisi aur node se aaya *AR= echo - real-time sync ke
        liye. Format: <ID>,<Seq>,*AR=00 1A <area> <level> <channel>

        NOTE: is line ke wire par pehle "001,086," jaisa prefix bhi ho sakta
        hai (Docklight/kisi aur client ka apna format) - hum bas "*AR=" ke
        baad ka hex nikaalte hain, prefix se koi farak nahi padta.
        """
        try:
            idx = line.find("*AR=")
            if idx == -1:
                return
            hex_part = line[idx + 4:idx + 14]
            if len(hex_part) < 10:
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 5 or b[1] != 0x1A:
                return
            area = b[2]
            level = b[3]
            ch_num = b[4]

            # Manual mode (Area configured, >0): sirf USI area ke frames
            # accept karo, aur sirf pehle-se-configured channels ki state
            # update karo - naye "phantom" channel apne aap mat bana do
            # (isi wajah se pehle ek 2-channel MOD2U par galti se ch3/ch4
            # bhi ban gaye the, kisi doosre device/area ke traffic se).
            if self._legacy_area and self._legacy_area > 0:
                if area != self._legacy_area:
                    return
                if ch_num not in self.channel_states:
                    _LOGGER.debug(
                        "Raylogic MOD2U %s: area=%d ch=%d ka *AR= frame "
                        "aaya lekin ye channel manual config mein nahi hai "
                        "- ignore kiya (kisi doosre device ka ho sakta hai).",
                        self.ip, area, ch_num,
                    )
                    return
                ch_type = self.channel_states[ch_num].get("type", CH_TYPE_RELAY)
                self.channel_states[ch_num].update(self._decode_level(ch_type, level))
                if self.state_callback:
                    self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
                return

            # LEARN mode (Area=0): naya channel discover hone par entity
            # dynamically bana do - ye purana intended behavior hai. LEARN
            # sirf Relay ke liye chalta hai (naya channel hamesha relay
            # maan kar banaya jaata hai), isliye yahan seedha Relay decode.
            is_new = self._learn_channel(ch_num, area)
            self.channel_states[ch_num]["on"] = f"{level:02X}" == RELAY_LEVEL_ON

            if is_new and self.new_channel_callback:
                self.new_channel_callback(ch_num, self.channel_states[ch_num])

            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
        except Exception as exc:
            _LOGGER.debug("MOD2U AR parse error '%s': %s", line, exc)
