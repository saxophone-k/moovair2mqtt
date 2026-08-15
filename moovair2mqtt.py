#!/usr/bin/env python3
"""
moovair2mqtt v3 — LOCAL bridge (no cloud, no Midea account).

Talks directly to the thermostat's Wi-Fi module over ADB:
  READ  : tail `logread` and parse dev_app / meiju state lines
  WRITE : inject into dev_app's SysV message queue (msqid 1) via `msgtool`

Protocol reference: local_recon/DEVICE_MAP.md, KV_MAP.md, COMMAND_MAP.md
Design spec:        local_recon/BRIDGE_NOTES.md

Everything here was verified against real hardware unless marked UNVERIFIED.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import struct
import sys
import threading
import time

import paho.mqtt.client as mqtt

try:
    from adb_shell.adb_device import AdbDeviceTcp
except ImportError:  # pragma: no cover
    AdbDeviceTcp = None

LOG = logging.getLogger("moovair-local")

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants — see DEVICE_MAP.md
# ─────────────────────────────────────────────────────────────────────────────

MSQID = 1
MSG_LEN = 1056                 # dev_app's fixed rac_queue message size

TYPE_TLV = 0x30000             # Channel A — `aa .. 44` TLV frame
TYPE_KV = 0                    # Channel B — key/value
TYPE_QUERY = 2                 # read-only state request (no setters fire)

# Channel B (KV) keys
KV_POWER = 0
KV_SETPOINT = 1                # float
KV_FAN = 2
KV_MODE = 3
KV_AUX_HEAT = 30               # setEleFunc   — PTC *with* heat pump
KV_EMERGENCY = 31              # setElecHeatAlone — PTC alone

# Channel A (TLV) tags
TLV_POWER = 1
TLV_MODE = 2
TLV_SETPOINT = 3
TLV_FAN = 4
TLV_DEHUM_INTERVAL = 0x71      # MUST precede the mode tag or it is dropped
# Emergency-heat tags, captured from the official app 2026-08-14. These are
# handled by `msmart_cmd_ctrl` — a DIFFERENT Channel-A sub-handler from
# `parse_tlv_hbs_cmd` — and it is the one that calls
# `rac_dev_notify_ui_state_update`, so the thermostat's screen updates.
TLV_AUX_41 = 0x41              # always 2 in the app's frames
TLV_EMERGENCY = 0x67           # 3 = emergency heat, 2 = normal
TLV_PTC = 0x1F                 # bit0 = ptc enabled: 0x0b normal, 0x0a emergency
# Captured from the app 2026-08-14. Same 2/3 encoding as TLV_EMERGENCY.
TLV_FREEZE = 0x41              # 3 = freeze protection (8 C) ON, 2 = OFF
TLV_TEMP_UNIT = 0x47           # 0 = Celsius, 1 = Fahrenheit

# ac_mode_t
MODE_AUTO, MODE_COOL, MODE_DRY, MODE_HEAT, MODE_FAN = 1, 2, 3, 4, 5

HVAC_FROM_MODE = {
    MODE_AUTO: "heat_cool",
    MODE_COOL: "cool",
    MODE_DRY: "cool",          # Option B: dry shows as cool, like the panel
    MODE_HEAT: "heat",
    MODE_FAN: "fan_only",
}
MODE_FROM_HVAC = {
    "heat_cool": MODE_AUTO,
    "cool": MODE_COOL,
    "heat": MODE_HEAT,
    "fan_only": MODE_FAN,
}

FAN_FROM_IDX = {0: "low", 1: "medium", 2: "high", 3: "auto"}
FAN_TO_IDX = {v: k for k, v in FAN_FROM_IDX.items()}
FAN_TLV = {0: 0x1E, 1: 0x3C, 2: 0x5A, 3: 0x66}

PRESET_NORMAL = "Normal"
PRESET_EMERGENCY = "Emergency Heat"

TEMP_OFFSET = 62               # UART sensor bytes: °C = raw - 62

# available_mode bitmask → which hvac modes the unit supports.
# The reference unit reports 0x7f. Bit meanings are INFERRED; when a bit is unknown
# we keep the mode rather than hide a working one.
AVAILABLE_MODE_BITS = {
    MODE_AUTO: 0x01, MODE_COOL: 0x02, MODE_DRY: 0x04,
    MODE_HEAT: 0x08, MODE_FAN: 0x10,
}


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def _env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        return None
    return val


class Config:
    def __init__(self):
        host = _env("M2M_THERMOSTAT_HOST", "")
        self.thermostat_host = ""
        self.thermostat_port = 5555
        if host:
            if ":" in host:
                h, p = host.rsplit(":", 1)
                self.thermostat_host, self.thermostat_port = h, int(p)
            else:
                self.thermostat_host = host

        self.mqtt_host = _env("M2M_MQTT_HOST", "")
        self.mqtt_port = int(_env("M2M_MQTT_PORT", "1883"))
        self.mqtt_user = _env("M2M_MQTT_USERNAME")
        self.mqtt_pass = _env("M2M_MQTT_PASSWORD")
        self.prefix = _env("M2M_MQTT_TOPIC_PREFIX", "moovair2mqtt")
        self.discovery_prefix = _env("M2M_HA_DISCOVERY_PREFIX", "homeassistant")
        self.log_level = _env("M2M_LOG_LEVEL", "info")
        self.cloud_mode = _env("M2M_CLOUD_MODE", "alongside")  # alongside|local_only
        # ⚠ Keep this LOW-FREQUENCY. The event stream is the primary path; this
        # query is only a safety net for missed events. Aggressive polling is a
        # suspect in the sensor-task wedges observed 2026-08-13/14 (humidity
        # going --% and the proximity sensor dying until a power cycle), so we
        # deliberately do not hammer the device. 0 disables it entirely.
        self.query_interval = float(_env("M2M_QUERY_INTERVAL", "60"))
        self.heartbeat_timeout = float(_env("M2M_HEARTBEAT_TIMEOUT", "30"))
        self.msgtool_local = _env("M2M_MSGTOOL_PATH", "/app/msgtool")
        self.msgtool_remote = "/tmp/msgtool"
        # Seeds every Home Assistant unique_id, so it must stay STABLE for the
        # life of the install: change it and HA creates a brand-new set of
        # entities, orphaning your history, dashboards and automations.
        #
        # Default: derived from the thermostat's address. Fine for most people
        # (give it a fixed IP so it never moves). If you ever do change the
        # thermostat's IP, set M2M_DEVICE_ID explicitly to the previous value.
        #
        # ⚠ Migrating from v2 (the cloud bridge)? Set M2M_DEVICE_ID to your
        # Midea appliance ID — the value v2 used — and your existing entities
        # carry straight over instead of being duplicated.
        self.device_id = (_env("M2M_DEVICE_ID")
                          or re.sub(r"[^0-9A-Za-z]", "_",
                                    self.thermostat_host or "unknown"))

    # ── v2 → v3 migration guard ──────────────────────────────────────────
    def check_legacy(self):
        legacy = [k for k in ("M2M_MOOVAIR_USERNAME", "M2M_MOOVAIR_PASSWORD")
                  if os.environ.get(k)]
        if not self.thermostat_host:
            bar = "═" * 68
            print(f"""
{bar}
  moovair2mqtt v3 — BREAKING CHANGE
  This version talks to your thermostat LOCALLY (no cloud, no account).

  {'Detected OLD v2 configuration: ' + ', '.join(legacy) if legacy else 'Missing required configuration.'}
    M2M_THERMOSTAT_HOST is MISSING and is now REQUIRED.

  What to do:
    1. Give your thermostat a fixed IP address (your router may call
       this a DHCP reservation) and note it down
    2. In docker-compose.yml remove the Moovair account variables and add:
         M2M_THERMOSTAT_HOST: "192.168.x.x:5555"
    3. Keep your existing Home Assistant entities by also setting
         M2M_DEVICE_ID: "<your Midea appliance id>"
       (without it the entities are recreated and you lose their history)
    4. docker compose up -d

  Prefer to stay on the cloud version?  Pin the image to  :2.1.0
  Full guide: https://github.com/saxophone-k/moovair2mqtt/blob/v3.0.0/MIGRATION.md
{bar}
""", file=sys.stderr)
            return False
        if legacy:
            LOG.warning("Ignoring v2 cloud variables (%s) — v3 is local-only "
                        "and needs no Midea account.", ", ".join(legacy))
        if not self.mqtt_host:
            print("M2M_MQTT_HOST is required.", file=sys.stderr)
            return False
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Message builders
# ─────────────────────────────────────────────────────────────────────────────

def _envelope(msg_type: int, body: bytes = b"", count: int = 0,
              body_len: int | None = None) -> bytes:
    """Build the 1056-byte rac_queue mtext, prefixed with mtype=1 for msgtool."""
    m = bytearray(MSG_LEN)
    m[0:4] = struct.pack("<I", 1)              # magic
    m[4:8] = struct.pack("<I", msg_type)       # ← selects the parser
    if count:
        m[9] = count                           # KV entry count
    if body_len is not None:
        m[8:12] = struct.pack("<I", body_len)  # TLV frame length
    m[12:12 + len(body)] = body
    return struct.pack("<i", 1) + bytes(m)


def build_kv(entries) -> bytes:
    """entries: [(key, value)] — float values are encoded as f32, ints as i32."""
    body = b""
    for key, val in entries:
        body += struct.pack("<I", key)
        body += struct.pack("<f", val) if isinstance(val, float) else \
            struct.pack("<i", int(val))
    return _envelope(TYPE_KV, body, count=len(entries))


def build_tlv(tags) -> bytes:
    """tags: [(tag, value)] in ORDER. 0x71 must come before the mode tag.

    ⚠ Header byte 11 is the **TLV COUNT**, not a constant. Learned by capturing
    the official app: it sends `02 02 d0 05` for 5 tags and `02 02 d0 02` for 2.
    We previously hardcoded 0x04 regardless, which was wrong for any frame that
    did not happen to carry exactly four tags.
    """
    header = bytes([0xAA, 0x00, 0x44, 0, 0, 0, 0, 0, 0x02, 0x02, 0xD0, len(tags)])
    body = b"".join(struct.pack("<I", t) + bytes([1, v & 0xFF]) for t, v in tags)
    frame = bytearray(header + body + b"\x00\x00" + b"\xB5\xEE\x3C")
    frame[1] = len(frame) - 1                  # length byte
    frame = bytes(frame)
    return _envelope(TYPE_TLV, frame, body_len=len(frame))


def build_query() -> bytes:
    return _envelope(TYPE_QUERY)


# ─────────────────────────────────────────────────────────────────────────────
# ADB transport — pure Python, no platform-tools needed
# ─────────────────────────────────────────────────────────────────────────────

class Device:
    """
    TWO independent ADB connections.

    ⚠ A single AdbDeviceTcp cannot serve a long-lived `logread -f` stream AND
    concurrent shell commands — they share one socket and corrupt each other,
    which shows up as multi-second command latency. So: one connection owns the
    read stream, a separate one owns writes.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dev = None          # write/command connection
        self.rdev = None         # dedicated read-stream connection
        self._lock = threading.Lock()

    def _new(self):
        if AdbDeviceTcp is None:
            raise RuntimeError("adb-shell is not installed (pip install adb-shell)")
        d = AdbDeviceTcp(self.cfg.thermostat_host, self.cfg.thermostat_port,
                         default_transport_timeout_s=15.0)
        d.connect(auth_timeout_s=1.0)
        return d

    def connect(self):
        self.dev = self._new()
        LOG.info("ADB (command channel) connected to %s:%s",
                 self.cfg.thermostat_host, self.cfg.thermostat_port)
        self.ensure_msgtool()

    def connect_reader(self):
        self.rdev = self._new()
        LOG.info("ADB (read stream) connected")
        return self.rdev

    def close(self):
        for attr in ("dev", "rdev"):
            try:
                d = getattr(self, attr)
                if d:
                    d.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def close_reader(self):
        try:
            if self.rdev:
                self.rdev.close()
        except Exception:
            pass
        self.rdev = None

    def close_command(self):
        try:
            if self.dev:
                self.dev.close()
        except Exception:
            pass
        self.dev = None

    def shell(self, cmd, timeout=15.0):
        with self._lock:
            return self.dev.shell(cmd, transport_timeout_s=timeout,
                                  read_timeout_s=timeout)

    def ensure_msgtool(self):
        """/tmp is volatile — re-push msgtool after every device reboot."""
        try:
            out = self.shell(f"[ -x {self.cfg.msgtool_remote} ] && echo ok")
        except Exception:
            out = ""
        if "ok" in (out or ""):
            return
        if not os.path.exists(self.cfg.msgtool_local):
            LOG.error("msgtool not found at %s — cannot send commands",
                      self.cfg.msgtool_local)
            return
        LOG.info("Pushing msgtool to the thermostat (%s)", self.cfg.msgtool_remote)
        with self._lock:
            self.dev.push(self.cfg.msgtool_local, self.cfg.msgtool_remote)
        self.shell(f"chmod +x {self.cfg.msgtool_remote}")

    def send(self, payload: bytes):
        self.ensure_msgtool()
        self.shell(f"{self.cfg.msgtool_remote} send {MSQID} {payload.hex()}")


# ─────────────────────────────────────────────────────────────────────────────
# Log parsing — see BRIDGE_NOTES.md "read path"
# ─────────────────────────────────────────────────────────────────────────────

RE_AHU3_A = re.compile(
    r"RAC_DEV_AHU3_STATE, power: (\d+), mode: (\d+).*?temp_unit =(\d+), "
    r"temp_set: ([\d.]+), wind_speed: (\d+)")
RE_AHU3_B = re.compile(
    r"RAC_DEV_AHU3_STATE, fan_state: (\d+).*?elec_heat: (\d+), "
    r"elec_heat_only:(\d+), compressor_state =(\d+)")
# ⚠ Use the COMPENSATED values meiju reports — these are what the PANEL shows.
# `get_dev_sensor_data f_temp` is the RAW sensor and reads ~2 °C high; publishing
# it made HA disagree with the thermostat's own display (caught 2026-08-14).
# ⚠ `AHU3_STATE ... temp_set:` TRUNCATES to whole degrees (21.5 -> 21.0), but
# the device really does support 0.5 steps. `setTemp .Ts =` carries the true
# value, so prefer it. Verified 2026-08-14: KV setpoint 21.5 -> setTemp 21.5
# while AHU3_STATE reported 21.0. (TLV tag 3 truncates too — use KV for
# setpoints.)
RE_SETTEMP = re.compile(r"setTemp .*?\.Ts = ([\d.]+)")
RE_SENSOR_MEIJU = re.compile(r"temp_val (\d+),\s*humi_val (\d+)")
# Raw sensor: used ONLY as a liveness heartbeat, never published.
RE_SENSOR_RAW = re.compile(r"f_temp = ([\d.]+)\s+sensor->humi_val = ([\d.]+)")
# NOTE: the countdown appears on TWO lines with DIFFERENT separators — the
# per-minute tick is on the meiju "notify" line which uses '='. Parse both.
RE_DEHUM = re.compile(r"dehum_time_left[:=](\d+), dehum_interval[:=](\d+)")
RE_UI = re.compile(
    r"heating_max=(\d+),\s+cooling_min=(\d+).*?aux_heat_open=(\d+).*?"
    r"comm_mode=(\d+).*?available_mode:([0-9a-fA-F]+)")
# `msmart_cmd_ctrl` logs this on every change — from HA, the app, or the panel —
# which is our only read-back for freeze protection (it is not in AHU3_STATE).
RE_FREEZE = re.compile(r"msmart set degree8Heat=(\d)")
RE_UART_C0 = re.compile(r"^\[aa c0 ((?:[0-9a-f]{2} )+)")


def f2c(f):
    return (f - 32.0) * 5.0 / 9.0


class State:
    """Everything we know. `dirty` marks fields changed since last publish."""

    def __init__(self):
        self.power = None
        self.mode = None
        self.setpoint = None
        self.fan = None
        self.fan_state = None
        self.elec_heat = None
        self.elec_heat_only = None
        self.compressor = None
        self.indoor_temp = None
        self.indoor_humidity = None
        self.humidity_fault = False
        self.coil_indoor = None       # T2A
        self.coil_outdoor = None      # T3
        self.board_indoor_temp = None  # T1 (board's own sensor)
        self.temp_unit = None          # 0 = C, 1 = F  (AHU3 `temp_unit`)
        self.freeze = False            # 8 C freeze protection
        self.dry_remaining = None
        self.dry_interval = None
        # capabilities (self-configuration)
        self.heating_max = None
        self.cooling_min = None
        self.aux_heat_open = None
        self.comm_mode = None
        self.available_mode = None
        self.last_seen = 0.0
        self.dirty = set()

    def set(self, field, value):
        # NOTE: None is a meaningful value for fields that can go "unavailable"
        # (e.g. indoor_humidity during the meiju 255 fault), so it is allowed.
        if value is None and field not in ("indoor_humidity",):
            return
        if getattr(self, field) != value:
            setattr(self, field, value)
            self.dirty.add(field)

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def hvac_mode(self):
        if self.power == 0:
            return "off"                     # off is POWER, not a mode value
        return HVAC_FROM_MODE.get(self.mode)

    @property
    def hvac_action(self):
        if self.power == 0:
            return "off"
        if self.elec_heat or (self.mode == MODE_HEAT and self.compressor):
            return "heating"
        if self.compressor and self.mode in (MODE_COOL, MODE_DRY):
            return "cooling"
        if self.mode == MODE_AUTO and self.compressor:
            return "cooling"                 # refined by four_valve if decoded
        if self.fan_state:
            return "fan"
        return "idle"

    @property
    def dry_active(self):
        return self.mode == MODE_DRY

    @property
    def preset(self):
        return PRESET_EMERGENCY if self.elec_heat_only else PRESET_NORMAL


def parse_line(line: str, st: State):
    m = RE_SETTEMP.search(line)
    if m:
        st.set("setpoint", float(m.group(1)))     # authoritative, keeps 0.5
        st.last_seen = time.time()
        return
    m = RE_AHU3_A.search(line)
    if m:
        st.set("power", int(m.group(1)))
        st.set("mode", int(m.group(2)))
        st.set("temp_unit", int(m.group(3)))
        sp = float(m.group(4))
        # Only accept the truncated value if it disagrees by a whole degree —
        # otherwise it would clobber a known .5 with its rounded self.
        if st.setpoint is None or abs(st.setpoint - sp) >= 1.0:
            st.set("setpoint", sp)
        st.set("fan", int(m.group(5)))
        st.last_seen = time.time()
        return
    m = RE_AHU3_B.search(line)
    if m:
        st.set("fan_state", int(m.group(1)))
        st.set("elec_heat", int(m.group(2)))
        st.set("elec_heat_only", int(m.group(3)))
        st.set("compressor", int(m.group(4)))
        return
    m = RE_SENSOR_MEIJU.search(line)
    if m:
        st.set("indoor_temp", int(m.group(1)) / 100.0)   # 2300 → 23.0 °C
        hum = int(m.group(2))
        # 255 = 0xFF = meiju's "invalid" sentinel. Known vendor bug: the sensor
        # reads fine but meiju publishes 255, and the panel shows "--%".
        # Recovery is `killall meiju`. See DEVICE_FAULTS.md. Report it as
        # unavailable rather than publishing a bogus 255 % into HA history.
        st.set("humidity_fault", hum == 255)
        st.set("indoor_humidity", None if hum == 255 else hum)
        st.last_seen = time.time()
        return
    m = RE_SENSOR_RAW.search(line)
    if m:
        st.last_seen = time.time()           # ~1/s → liveness heartbeat only
        return
    m = RE_DEHUM.search(line)
    if m:
        st.set("dry_remaining", int(m.group(1)))
        st.set("dry_interval", int(m.group(2)))
        return
    m = RE_UI.search(line)
    if m:
        st.set("heating_max", int(m.group(1)))
        st.set("cooling_min", int(m.group(2)))
        st.set("aux_heat_open", int(m.group(3)))
        st.set("comm_mode", int(m.group(4)))
        st.set("available_mode", int(m.group(5), 16))
        return
    m = RE_FREEZE.search(line)
    if m:
        st.set("freeze", m.group(1) == "1")
        return
    m = RE_UART_C0.search(line)
    if m:
        # group(1) begins AFTER "[aa c0 ", so b[i] == frame byte (i + 2).
        b = [int(x, 16) for x in m.group(1).split()]
        if len(b) >= 13:                       # need frame[14] → b[12]
            st.set("board_indoor_temp", b[11 - 2] - TEMP_OFFSET)  # T1  frame[11]
            st.set("coil_indoor", b[12 - 2] - TEMP_OFFSET)        # T2A frame[12]
            st.set("coil_outdoor", b[14 - 2] - TEMP_OFFSET)       # T3  frame[14]
        return


# ─────────────────────────────────────────────────────────────────────────────
# MQTT / Home Assistant
# ─────────────────────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State()
        # Guards `state` against torn reads. The reader thread rewrites these
        # fields continuously while the command thread assembles frames from
        # them; see `_full_tlv` for why a half-updated snapshot is dangerous.
        # (Device._lock is unrelated — that one serialises ADB sends.)
        self._state_lock = threading.Lock()
        self.device = Device(cfg)
        self.cmd_q: queue.Queue = queue.Queue()
        self.mqtt = None
        self._discovery_done = False
        self._stop = threading.Event()
        self._device_online = None      # None = unknown yet
        self._pending = {}              # field -> (value, sent_at) for latency

    # ── topics ───────────────────────────────────────────────────────────
    def t(self, leaf):
        return f"{self.cfg.prefix}/{leaf}"

    def disc(self, component, obj):
        return (f"{self.cfg.discovery_prefix}/{component}/"
                f"moovair_{self.cfg.device_id}/{obj}/config")

    # ── MQTT plumbing ────────────────────────────────────────────────────
    def start_mqtt(self):
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="moovair-local")
        except (AttributeError, TypeError):       # paho 1.x
            client = mqtt.Client(client_id="moovair-local")
        if self.cfg.mqtt_user:
            client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_pass or "")
        client.will_set(self.t("availability"), "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, 60)
        client.loop_start()
        self.mqtt = client

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        LOG.info("MQTT connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
        client.subscribe(self.t("set/#"))
        if self._device_online:
            client.publish(self.t("availability"), "online", retain=True)
        self._discovery_done = False

    def _on_message(self, client, userdata, msg):
        leaf = msg.topic.split("/set/", 1)[-1]
        payload = msg.payload.decode(errors="replace").strip()
        LOG.info("command: %s = %s", leaf, payload)
        self.cmd_q.put((leaf, payload))

    def set_device_online(self, online: bool, why: str = ""):
        """Availability must reflect the THERMOSTAT, not just the bridge.
        The MQTT will only covers the bridge dying; if the device goes away
        (power cut, WiFi drop) we must publish 'offline' ourselves."""
        if self._device_online == online:
            return
        self._device_online = online
        if self.mqtt and self.mqtt.is_connected():
            self.mqtt.publish(self.t("availability"),
                              "online" if online else "offline", retain=True)
        LOG.warning("thermostat %s%s", "ONLINE" if online else "OFFLINE",
                    f" ({why})" if why else "")

    def pub(self, leaf, value, retain=True):
        if value is None:
            return
        self.mqtt.publish(self.t(leaf), str(value), retain=retain)

    # ── HA discovery (self-configuring) ──────────────────────────────────
    def publish_discovery(self):
        st = self.state
        dev = {
            "identifiers": [f"moovair_{self.cfg.device_id}"],
            "name": "Moovair ST-1",
            "manufacturer": "Moovair / Midea",
            "model": "ST-1 Zone Controller (local)",
        }
        avail = {"availability_topic": self.t("availability"),
                 "payload_available": "online",
                 "payload_not_available": "offline"}

        modes = ["off"]
        for m, name in HVAC_FROM_MODE.items():
            if m == MODE_DRY:
                continue                       # Option B — dry is not an HA mode
            if st.available_mode is None or \
               st.available_mode & AVAILABLE_MODE_BITS.get(m, 0):
                if name not in modes:
                    modes.append(name)

        climate = {
            "name": "Moovair", "unique_id": f"moovair_{self.cfg.device_id}",
            "device": dev, **avail,
            "modes": modes,
            "fan_modes": ["auto", "low", "medium", "high"],
            "preset_modes": [PRESET_NORMAL, PRESET_EMERGENCY],
            "current_temperature_topic": self.t("current_temperature"),
            "temperature_state_topic": self.t("target_temperature"),
            "mode_state_topic": self.t("mode"),
            "fan_mode_state_topic": self.t("fan_mode"),
            "preset_mode_state_topic": self.t("preset"),
            "action_topic": self.t("action"),
            "temperature_command_topic": self.t("set/target_temperature"),
            "mode_command_topic": self.t("set/mode"),
            "fan_mode_command_topic": self.t("set/fan_mode"),
            "preset_mode_command_topic": self.t("set/preset"),
            "min_temp": st.cooling_min if st.cooling_min else 16,
            "max_temp": st.heating_max if st.heating_max else 30,
            "temp_step": 1.0,
            # follow the device (AHU3 `temp_unit`, tag 0x47: 0=C, 1=F).
            # Verified 2026-08-15: switched to °F at the panel and HA followed.
            "temperature_unit": "F" if st.temp_unit else "C",
        }
        self.mqtt.publish(self.disc("climate", "climate"), json.dumps(climate),
                          retain=True)

        def sensor(obj, name, topic, unit=None, dclass=None, icon=None):
            cfg = {"name": name, "unique_id": f"moovair_{self.cfg.device_id}_{obj}",
                   "device": dev, **avail, "state_topic": self.t(topic)}
            if unit:
                cfg["unit_of_measurement"] = unit
            if dclass:
                cfg["device_class"] = dclass
            if icon:
                cfg["icon"] = icon
            self.mqtt.publish(self.disc("sensor", obj), json.dumps(cfg), retain=True)

        def binary(obj, name, topic, icon):
            cfg = {"name": name, "unique_id": f"moovair_{self.cfg.device_id}_{obj}",
                   "device": dev, **avail, "state_topic": self.t(topic),
                   "payload_on": "ON", "payload_off": "OFF", "icon": icon}
            self.mqtt.publish(self.disc("binary_sensor", obj), json.dumps(cfg),
                              retain=True)

        sensor("indoor_humidity", "Indoor Humidity", "indoor_humidity", "%",
               "humidity")
        sensor("outdoor_temp", "Outdoor Coil Temperature", "outdoor_temperature",
               "°C", "temperature")
        sensor("indoor_coil_temp", "Indoor Coil Temperature", "indoor_coil_temperature",
               "°C", "temperature")
        sensor("dry_remain", "Dry Mode Remaining", "dry_mode_remaining", "min",
               icon="mdi:timer-sand")
        binary("aux_heat", "Aux Heat", "aux_heat", "mdi:heating-coil")
        binary("heat_pump", "Heat Pump", "heat_pump", "mdi:heat-pump")

        # Dry mode: button + duration, mirroring the panel (Option B)
        num = {"name": "Dry Duration",
               "unique_id": f"moovair_{self.cfg.device_id}_dry_duration",
               "device": dev, **avail,
               "state_topic": self.t("dry_duration"),
               "command_topic": self.t("set/dry_duration"),
               "min": 5, "max": 120, "step": 1,
               "unit_of_measurement": "min", "icon": "mdi:water-percent"}
        self.mqtt.publish(self.disc("number", "dry_duration"), json.dumps(num),
                          retain=True)

        frz = {"name": "Freeze Protection",
               "unique_id": f"moovair_{self.cfg.device_id}_freeze",
               "device": dev, **avail,
               "state_topic": self.t("freeze_protection"),
               "command_topic": self.t("set/freeze_protection"),
               "payload_on": "ON", "payload_off": "OFF",
               "icon": "mdi:snowflake-alert"}
        self.mqtt.publish(self.disc("switch", "freeze_protection"),
                          json.dumps(frz), retain=True)

        sw = {"name": "Dry Mode",
              "unique_id": f"moovair_{self.cfg.device_id}_dry_mode",
              "device": dev, **avail,
              "state_topic": self.t("dry_mode"),
              "command_topic": self.t("set/dry_mode"),
              "payload_on": "ON", "payload_off": "OFF",
              "icon": "mdi:water-percent"}
        self.mqtt.publish(self.disc("switch", "dry_mode"), json.dumps(sw),
                          retain=True)

        LOG.info("HA discovery published (modes=%s, range=%s-%s)", modes,
                 climate["min_temp"], climate["max_temp"])
        self._discovery_done = True

    # ── publish state ────────────────────────────────────────────────────
    def _confirm(self):
        """Log the round-trip: MQTT command -> device state actually changed."""
        st = self.state
        for leaf, (want, sent) in list(self._pending.items()):
            got = None
            if leaf == "mode":
                got = st.hvac_mode
            elif leaf == "target_temperature":
                got = str(st.setpoint)
                want = str(float(want))
            elif leaf == "fan_mode":
                got = FAN_FROM_IDX.get(st.fan)
            elif leaf == "preset":
                got = st.preset
                if want in ("none", "None", ""):
                    want = PRESET_NORMAL
            elif leaf == "dry_mode":
                got = "ON" if st.dry_active else "OFF"
                want = str(want).upper()
            if got is not None and str(got) == str(want):
                LOG.info("✓ device confirmed %s=%s after %.0f ms", leaf, want,
                         (time.time() - sent) * 1000)
                self._pending.pop(leaf, None)
            elif time.time() - sent > 20:
                LOG.warning("✗ device did NOT confirm %s=%s within 20s "
                            "(device reports %s)", leaf, want, got)
                self._pending.pop(leaf, None)

    def publish_state(self, force=False):
        st = self.state
        self._confirm()
        if not force and not st.dirty:
            return
        if st.hvac_mode:
            self.pub("mode", st.hvac_mode)
            self.pub("action", st.hvac_action)
        if st.setpoint is not None:
            self.pub("target_temperature", st.setpoint)
        if st.fan is not None:
            self.pub("fan_mode", FAN_FROM_IDX.get(st.fan, "auto"))
        self.pub("preset", st.preset)
        self.pub("current_temperature", st.indoor_temp)
        self.pub("indoor_humidity",
                 "unavailable" if st.humidity_fault else st.indoor_humidity)
        self.pub("outdoor_temperature", st.coil_outdoor)
        self.pub("indoor_coil_temperature", st.coil_indoor)
        self.pub("dry_mode", "ON" if st.dry_active else "OFF")
        self.pub("freeze_protection", "ON" if st.freeze else "OFF")
        self.pub("dry_mode_remaining", st.dry_remaining)
        self.pub("dry_duration", st.dry_interval)
        if st.elec_heat is not None:
            self.pub("aux_heat", "ON" if st.elec_heat else "OFF")
        if st.compressor is not None:
            self.pub("heat_pump", "ON" if st.compressor else "OFF")
        st.dirty.clear()

    # ── command handling ─────────────────────────────────────────────────
    def _full_tlv(self, **over):
        """Channel A frames are sent complete — proven safe. Order matters.

        ⚠ Every frame carries power + setpoint + fan + mode, so the three fields
        the caller did NOT set are filled in from current state. Reading them
        one at a time lets the reader thread change state mid-read, producing a
        frame that mixes two different moments — e.g. a new setpoint sent
        alongside a stale mode, snapping the thermostat back to where it was.
        The window is microseconds and has never been observed, but the failure
        would be silent and baffling, so take ONE consistent snapshot instead.
        """
        with self._state_lock:
            cur_power = self.state.power
            cur_setpoint = self.state.setpoint
            cur_fan = self.state.fan
            cur_mode = self.state.mode

        if "power" in over:
            power = over["power"]
        elif cur_power is None:
            power = 0x03                     # state unknown yet — assume on
        else:
            # ⚠ Must PRESERVE the current power state. `st.power or 1` was always
            # truthy, so changing the setpoint or fan while the unit was OFF
            # silently switched it ON. Only reachable once core controls moved to
            # Channel A (2026-08-14).
            power = 0x03 if cur_power else 0x00
        sp = over.get("setpoint", cur_setpoint or 22.0)
        fan = over.get("fan", cur_fan if cur_fan is not None else 3)
        mode = over.get("mode", cur_mode or MODE_COOL)
        tags = [(TLV_POWER, power),
                (TLV_SETPOINT, int(round(sp * 2 + 50))),
                (TLV_FAN, FAN_TLV.get(fan, 0x66))]
        if "dehum" in over:                      # MUST precede the mode tag
            tags.append((TLV_DEHUM_INTERVAL, over["dehum"]))
        tags.append((TLV_MODE, mode))
        return build_tlv(tags)

    def _ui_repaint(self):
        """Force meiju to redraw the thermostat's screen.

        KV (Channel B) commands do NOT call `rac_dev_notify_ui_state_update`, so
        anything sent only over KV — emergency heat (31), dry entry — is applied
        by the system but never shown on the panel. A **no-op Channel A frame**
        (current power/mode/setpoint/fan) does notify, so it repaints without
        changing anything. Observed 2026-08-14: preset changes didn't show until
        a mode command happened to follow.
        """
        try:
            time.sleep(0.3)
            self.device.send(self._full_tlv())
        except Exception as exc:
            LOG.debug("ui repaint failed: %s", exc)

    def handle_command(self, leaf, payload):
        """
        ⚠ CHANNEL CHOICE MATTERS FOR THE THERMOSTAT'S OWN SCREEN.

        `rac_dev_kv_parse` (Channel B) does NOT call
        `rac_dev_notify_ui_state_update`, so meiju — which paints the panel —
        never learns about the change and the display stays stale until the
        compressor cycles (a board frame, which does notify).
        `msmart_cmd_ctrl` (Channel A, the `aa` TLV frame) DOES notify.

        So: power / mode / setpoint / fan go over **Channel A**.
        Channel B is used only where there is no TLV equivalent
        (emergency heat = KV 31) or where TLV proved unreliable (dry entry).
        Diagnosed 2026-08-14 after noticing the panel only refreshed on
        compressor state changes.
        """
        st = self.state

        if leaf == "mode":
            if payload == "off":
                self.device.send(self._full_tlv(power=0x00))
                return
            mode = MODE_FROM_HVAC.get(payload)
            if mode is None:
                LOG.warning("unknown mode %r", payload)
                return
            # leaving heat clears emergency, exactly as the app does
            if st.elec_heat_only and mode != MODE_HEAT:
                self.device.send(build_kv([(KV_EMERGENCY, 0)]))
            self.device.send(self._full_tlv(power=0x03, mode=mode))

        elif leaf == "target_temperature":
            # whole degrees only — the panel and the Moovair app cannot
            # display halves, even though dev_app accepts them internally.
            sp = int(round(float(payload)))
            self.device.send(self._full_tlv(setpoint=sp))

        elif leaf == "fan_mode":
            idx = FAN_TO_IDX.get(payload)
            if idx is None:
                return
            if st.mode == MODE_AUTO:
                LOG.info("Auto mode forces the fan to auto — the device will "
                         "override this request (known behaviour).")
            self.device.send(self._full_tlv(fan=idx))

        elif leaf == "preset":
            # Replay the OFFICIAL APP's frame (captured 2026-08-14). KV key 31
            # also sets the flag, but it does not notify meiju so the panel
            # stays stale; this path goes through `msmart_cmd_ctrl`, which does.
            emerg = (payload == PRESET_EMERGENCY)
            # Emergency implies heat. Leaving it must NOT drag the user out of
            # whatever mode they are in, so reuse the current mode.
            mode = MODE_HEAT if emerg else (st.mode or MODE_HEAT)
            power = 0x03 if (st.power is None or st.power) else 0x00
            self.device.send(build_tlv([
                (TLV_MODE, mode),
                (TLV_AUX_41, 2),
                (TLV_EMERGENCY, 3 if emerg else 2),
                (TLV_POWER, power),
                (TLV_PTC, 0x0A if emerg else 0x0B),
            ]))

        elif leaf == "freeze_protection":
            # Captured from the app: tag 0x41, 3 = on / 2 = off. Freeze
            # protection drives the unit to 8 C, so the app also restores the
            # real setpoint when switching it OFF — we do the same.
            on = payload.upper() in ("ON", "1", "TRUE")
            if on:
                self._freeze_restore = st.setpoint
                self.device.send(build_tlv([(TLV_FREEZE, 3)]))
            else:
                tags = [(TLV_FREEZE, 2)]
                back = getattr(self, "_freeze_restore", None) or st.setpoint
                if back:
                    tags.append((TLV_SETPOINT, int(round(back * 2 + 50))))
                self.device.send(build_tlv(tags))

        elif leaf == "dry_duration":
            minutes = max(1, min(255, int(float(payload))))
            self.device.send(self._full_tlv(dehum=minutes))

        elif leaf == "dry_mode":
            if payload.upper() == "ON":
                if st.mode not in (MODE_COOL, MODE_DRY):
                    LOG.info("Dry requires cool mode — switching to cool first.")
                    self.device.send(self._full_tlv(mode=MODE_COOL))
                    time.sleep(1.0)
                if st.dry_interval:
                    self.device.send(self._full_tlv(dehum=st.dry_interval))
                    time.sleep(0.5)
                # TLV mode->dry proved unreliable; KV is the dependable route in
                self.device.send(build_kv([(KV_MODE, MODE_DRY)]))
                self._ui_repaint()
            else:
                self.device.send(self._full_tlv(mode=MODE_COOL))
                if st.dry_interval:
                    time.sleep(0.5)
                    self.device.send(self._full_tlv(dehum=st.dry_interval))
        else:
            LOG.warning("unhandled command topic %r", leaf)

    # ── main loops ───────────────────────────────────────────────────────
    def reader_loop(self):
        """Tail logread on its OWN connection; reconnect on failure."""
        while not self._stop.is_set():
            try:
                rdev = self.device.connect_reader()
                LOG.info("tailing logread")
                for chunk in rdev.streaming_shell("logread -f",
                                                  transport_timeout_s=None):
                    if self._stop.is_set():
                        break
                    # One acquire per chunk, not per line: `logread` delivers
                    # ~200 lines/s and parse_line only mutates state (it cannot
                    # publish or block), so holding it across the chunk is cheap
                    # and keeps the command thread from seeing a partial update.
                    with self._state_lock:
                        for line in chunk.splitlines():
                            parse_line(line, self.state)
                    if chunk:
                        self.set_device_online(True, "log stream active")
            except Exception as exc:
                LOG.warning("read stream lost (%s) — reconnecting in 5s", exc)
                self.set_device_online(False, "read stream lost")
                self.device.close_reader()
                self._stop.wait(5)

    def _ensure_command_channel(self):
        """The write connection is separate from the read stream and must be
        established (and re-established) independently."""
        if self.device.dev is not None:
            return True
        try:
            self.device.connect()
            self.device.send(build_query())          # resync on (re)connect
            return True
        except Exception as exc:
            LOG.warning("command channel unavailable (%s)", exc)
            self.device.dev = None
            return False

    def worker_loop(self):
        last_query = 0.0
        last_conn_try = 0.0
        while not self._stop.is_set():
            now = time.time()
            if self.device.dev is None and now - last_conn_try >= 5:
                last_conn_try = now
                self._ensure_command_channel()

            # commands first — responsiveness matters
            try:
                leaf, payload = self.cmd_q.get(timeout=0.2)
                try:
                    if not self._ensure_command_channel():
                        raise RuntimeError("no command channel")
                    t0 = time.time()
                    self.handle_command(leaf, payload)
                    LOG.info("command %s=%s injected in %.0f ms", leaf, payload,
                             (time.time() - t0) * 1000)
                    self._pending[leaf] = (payload, time.time())
                except Exception as exc:
                    LOG.error("command %s failed: %s", leaf, exc)
                    self.device.close_command()      # force a fresh connection
            except queue.Empty:
                pass

            now = time.time()
            if self.state.last_seen and \
                    now - self.state.last_seen > self.cfg.heartbeat_timeout:
                LOG.warning("no sensor data for %.0fs — the log stream looks "
                            "dead; forcing reconnect",
                            now - self.state.last_seen)
                self.set_device_online(False, "no sensor data")
                self.state.last_seen = 0
                self.device.close()

            if self.cfg.query_interval > 0 and self.device.dev and \
                    now - last_query >= self.cfg.query_interval:
                last_query = now
                try:
                    self.device.send(build_query())     # READ-ONLY resync
                except Exception as exc:
                    LOG.debug("query failed: %s", exc)

            if self.mqtt and self.mqtt.is_connected():
                if not self._discovery_done and self.state.mode is not None:
                    self.publish_discovery()
                self.publish_state()

    def run(self):
        self.start_mqtt()
        threading.Thread(target=self.reader_loop, daemon=True).start()
        try:
            self.worker_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            if self.mqtt:
                self.mqtt.publish(self.t("availability"), "offline", retain=True)
                self.mqtt.loop_stop()
            self.device.close()


def main():
    cfg = Config()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    # ⚠ Set OUR logger only. Configuring the root logger at DEBUG makes
    # `adb_shell` log every ADB packet — and `logread` delivers ~200 lines/s, so
    # the process drowns in log formatting and commands crawl. That was the
    # cause of the "super laggy control" seen 2026-08-14.
    LOG.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    for noisy in ("adb_shell", "adb_shell.adb_device", "adb_shell.transport",
                  "paho", "paho.mqtt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not cfg.check_legacy():
        sys.exit(1)
    if cfg.cloud_mode == "local_only":
        LOG.info("cloud_mode=local_only — the panel's weather icon will not "
                 "work (it is AccuWeather data fetched via the cloud).")
    LOG.info("moovair2mqtt v3 (local) starting — thermostat %s:%s",
             cfg.thermostat_host, cfg.thermostat_port)
    Bridge(cfg).run()


if __name__ == "__main__":
    main()
