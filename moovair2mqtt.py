#!/usr/bin/env python3
"""
moovair2mqtt — Bridge Moovair ST-1 (thermopompe) ↔ MQTT ↔ Home Assistant
Suit la même convention que mysa2mqtt (variables M2M_*).
"""

import asyncio
import hashlib
import json
import logging
import os
import signal
import struct
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus, urlencode, urlparse

import httpx
import paho.mqtt.client as mqtt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from firebase_messaging import FcmPushClient, FcmRegisterConfig

# ── Configuration depuis variables d'environnement ───────────────────────────

def _env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise SystemExit(f"Variable d'environnement requise manquante: {key}")
    return val

CFG = {
    "moovair_email":    _env("M2M_MOOVAIR_USERNAME", required=True),
    "moovair_password": _env("M2M_MOOVAIR_PASSWORD", required=True),
    "mqtt_host":        _env("M2M_MQTT_HOST", required=True),
    "mqtt_port":        int(_env("M2M_MQTT_PORT", "1883")),
    "mqtt_user":        _env("M2M_MQTT_USERNAME", ""),
    "mqtt_pass":        _env("M2M_MQTT_PASSWORD", ""),
    "mqtt_prefix":      _env("M2M_MQTT_TOPIC_PREFIX", "moovair2mqtt"),
    "poll_interval":    int(_env("M2M_POLL_INTERVAL", "30")),
    "log_level":        _env("M2M_LOG_LEVEL", "info").upper(),
}

logging.basicConfig(
    level=getattr(logging, CFG["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("moovair2mqtt")

# ── Constantes protocole Moovair / Midea NetHomePlus ─────────────────────────

BASE_URL     = "https://mapp-us.appsmb.com"
APP_ID       = "1244"
APP_KEY      = "51b9a382052143058fda97925a423a93"
SRC          = "17"
APPLIANCE_ID = None   # découvert au login
DEVICE_SN    = "bridge"

# ── Firebase Cloud Messaging (température ambiante via push) ──────────────────
FCM_SENDER_ID   = "1016425309209"
FCM_APP_ID      = "1:1016425309209:android:a397210f6f94bc2d7f3688"
FCM_PROJECT_ID  = "moovair"
FCM_API_KEY     = "AIzaSyA7HhxqQhZ5zfc1euu-HK6B3CZfAtNjrek"
FCM_MAX_SILENCE = 600   # secondes sans push → force reconnexion
FCM_TEMP_MAX_AGE = 900  # secondes : ne pas publier une temp plus vieille que ça

FAN_READ  = {30: "low", 60: "medium", 90: "high", 102: "auto"}
FAN_WRITE = {"low": 30, "medium": 60, "high": 90, "auto": 102}
MODE_WRITE = {"heat_cool": 1, "cool": 2, "dry": 3, "heat": 4,
              "emergency_heat": 4, "fan_only": 5}

DRY_MODE_OPTIONS  = ["off", "15 min", "20 min", "45 min", "60 min"]
DRY_MODE_DURATIONS = {"off": 0, "15 min": 15, "20 min": 20, "45 min": 45, "60 min": 60}
DRY_MODE_REQUIRES = ("cool", "heat_cool")  # modes qui permettent le dry mode

def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

def _sign(path, params):
    query = unquote_plus(urlencode(sorted(params.items())))
    return hashlib.sha256((path + query + APP_KEY).encode()).hexdigest()

def _aes_dec_session_key(access_token_hex):
    key = hashlib.md5(APP_KEY.encode()).hexdigest()[:16].encode()
    ct  = bytes.fromhex(access_token_hex)
    return unpad(AES.new(key, AES.MODE_ECB).decrypt(ct), 16).decode()

def _aes_enc(text, sk):
    return AES.new(sk.encode(), AES.MODE_ECB).encrypt(pad(text.encode(), 16)).hex()

def _aes_dec(hex_data, sk):
    ct = bytes.fromhex(hex_data)
    return unpad(AES.new(sk.encode(), AES.MODE_ECB).decrypt(ct), 16).decode()

def _fcm_decrypt_push(msg_hex, user_id):
    """Déchiffre un message push FCM Moovair → retourne les bytes payload Midea."""
    if not msg_hex:
        return None
    try:
        key = hashlib.md5((user_id + APP_KEY).encode()).hexdigest()[:16].encode()
        ct  = bytes.fromhex(msg_hex)
        dec = unpad(AES.new(key, AES.MODE_ECB).decrypt(ct), 16).decode()
        nums   = [int(x) for x in dec.split(',') if x.strip()]
        m0b    = bytes([n & 0xFF for n in nums])
        total  = struct.unpack('<H', m0b[4:6])[0]
        return m0b[40:40 + (total - 56)]
    except Exception:
        return None


# ── Client Cloud Moovair ──────────────────────────────────────────────────────

class MoovairCloud:
    def __init__(self):
        self.session_id   = ""
        self.session_key  = ""
        self._client      = httpx.AsyncClient(timeout=15.0)

    def _auth_body(self, extra=None):
        body = {"appId": APP_ID, "clientType": "1", "format": "2",
                "stamp": _ts(), "language": "en_US", "src": SRC}
        if extra:
            body.update(extra)
        return body

    async def _post(self, endpoint, body):
        body["sign"] = _sign(endpoint, body)
        r = await self._client.post(f"{BASE_URL}{endpoint}", data=body)
        resp = r.json()
        if str(resp.get("errorCode", resp.get("code", "1"))) != "0":
            raise RuntimeError(f"{endpoint} → {resp.get('msg', resp)}")
        return resp.get("result") or resp.get("data")

    async def login(self):
        log.info("Login Moovair cloud...")
        # 1) Obtenir le loginId
        result = await self._post("/v1/user/login/id/get",
            self._auth_body({"loginAccount": CFG["moovair_email"]}))
        login_id = result["loginId"]

        # 2) Calculer le password hash: SHA256(loginId + SHA256(password) + APP_KEY)
        pw_hash = hashlib.sha256(CFG["moovair_password"].encode()).hexdigest()
        full    = (login_id + pw_hash + APP_KEY).encode()
        password = hashlib.sha256(full).hexdigest()

        # 3) Login
        result = await self._post("/v1/user/login",
            self._auth_body({"loginAccount": CFG["moovair_email"],
                             "password":      password}))

        self.session_id  = result["sessionId"]
        self.session_key = _aes_dec_session_key(result["accessToken"])
        self.user_id     = result.get("userId", "")
        log.info("Login OK — sessionKey: %s…", self.session_key[:8])

    async def get_appliance_id(self):
        body = {"format": "2", "stamp": _ts(), "language": "en_US",
                "src": SRC, "sessionId": self.session_id}
        result = await self._post("/v1/appliance/user/list/get", body)
        devices = result.get("list", [])
        if not devices:
            raise RuntimeError("Aucun appareil trouvé dans le compte Moovair")
        dev = devices[0]
        log.info("Appareil trouvé: id=%s type=%s name=%s online=%s",
                 dev["id"], dev.get("type"), dev.get("name"), dev.get("onlineStatus"))
        return dev["id"], dev

    async def _lua_request(self, service_url, body_dict):
        lua_json = json.dumps(body_dict, separators=(",", ":"))
        data_hex = _aes_enc(lua_json, self.session_key)
        endpoint = "/v1/app2base/data/transmit"
        form = {"format": "2", "stamp": _ts(), "language": "en_US",
                "src": SRC, "sessionId": self.session_id,
                "proType": "0x01", "data": data_hex, "serviceUrl": service_url}
        form["sign"] = _sign(endpoint, form)
        r = await self._client.post(f"{BASE_URL}{endpoint}?serviceUrl={service_url}",
                                    data=form)
        resp = r.json()
        if str(resp.get("errorCode")) != "0":
            raise RuntimeError(f"lua_request {service_url}: {resp}")
        raw = resp.get("result", {})
        if isinstance(raw, dict):
            raw = raw.get("returnData", "")
        if not raw:
            raise RuntimeError(f"lua_request {service_url}: réponse vide")
        return json.loads(_aes_dec(raw, self.session_key))

    async def _transparent_send(self, appliance_id, cmd_bytes_str):
        raw_bytes = bytes([int(b, 16) for b in cmd_bytes_str.split(",")])
        now  = time.localtime()
        ms   = int((time.time() % 1) * 1000)
        h    = bytes([ms & 0xFF, now.tm_sec, now.tm_min, now.tm_hour,
                      now.tm_mday, now.tm_mon - 1, now.tm_year % 100, now.tm_year // 100])
        i    = int(appliance_id).to_bytes(8, 'little')[:6]
        tlen = len(raw_bytes) + 56
        m0   = (bytes([0x5A, 0x5A, 1, 0])
                + struct.pack('<H', tlen) + struct.pack('<H', 32)
                + struct.pack('<I', 1) + h + i
                + bytes(8) + bytes(6) + raw_bytes + bytes(16))
        signed = ",".join(str((b - 256) if b > 127 else b) for b in m0)
        enc    = _aes_enc(signed, self.session_key)
        body   = {"format": "2", "stamp": _ts(), "language": "en_US",
                  "src": SRC, "sessionId": self.session_id,
                  "applianceId": appliance_id, "funId": "0008", "order": enc}
        body["sign"] = _sign("/v1/appliance/transparent/send", body)
        r = await self._client.post(f"{BASE_URL}/v1/appliance/transparent/send", data=body)
        resp = r.json()
        if str(resp.get("errorCode")) != "0":
            raise RuntimeError(f"transparent_send: {resp}")
        reply_dec = _aes_dec(resp["result"]["reply"], self.session_key)
        m0_bytes  = bytes([int(v) & 0xFF for v in reply_dec.split(",")])
        total     = struct.unpack('<H', m0_bytes[4:6])[0]
        return m0_bytes[40:40 + (total - 56)]

    async def read_state(self, appliance_id):
        result = await self._lua_request("/v1/luacontrol/json2data", {
            "query": {"query_type": "query_all,display_status_query,central_control_special_data_query,indoor_run_status"},
            "deviceinfo": {"deviceSubType": "0x44", "deviceSN": DEVICE_SN},
        })
        payload = await self._transparent_send(appliance_id, result["result"])
        return _decode_state(payload)

    async def send_control(self, appliance_id, *, setpoint_c, hvac_mode, fan_mode="auto"):
        mode_lua = MODE_WRITE.get(hvac_mode, 4)
        fan_lua  = FAN_WRITE.get(fan_mode, 102)
        power    = "off" if hvac_mode == "off" else "on"
        separate_ptc = "on" if hvac_mode == "emergency_heat" else "off"
        result = await self._lua_request("/v1/luacontrol/json2data", {
            "control": {
                "power": power, "mode": mode_lua,
                "temperature": float(setpoint_c), "wind_speed": fan_lua,
                "separate_ptc_mode_switch": separate_ptc,
            },
            "status": "",
            "deviceinfo": {"deviceSubType": "0x44", "deviceSN": DEVICE_SN},
        })
        await self._transparent_send(appliance_id, result["result"])

    async def send_dry_mode(self, appliance_id, duration_min: int):
        """Active ou désactive le dry mode avec une durée en minutes (0 = off)."""
        if duration_min == 0:
            control = {"smart_dry_switch": 0}
        else:
            control = {"smart_dry_switch": 1, "dry_time_interval": duration_min}
        result = await self._lua_request("/v1/luacontrol/json2data", {
            "control": control,
            "status": "",
            "deviceinfo": {"deviceSubType": "0x44", "deviceSN": DEVICE_SN},
        })
        await self._transparent_send(appliance_id, result["result"])

    async def close(self):
        await self._client.aclose()


def _decode_state(payload):
    # payload[16] est une valeur fixe non représentative — température vient du FCM
    setpoint_c = (payload[22] - 50) / 2
    power      = payload[17]
    mode_byte  = payload[21]
    heat_pump  = payload[18] == 8
    fan_raw    = payload[23]
    heating    = bool(payload[85]) if len(payload) > 85 else False

    if power == 0:
        hvac_mode = "off"
        action    = "off"
    elif mode_byte == 1: hvac_mode = "heat_cool";      action = "heating" if heating else "idle"
    elif mode_byte == 2: hvac_mode = "cool";            action = "cooling" if heating else "idle"
    elif mode_byte == 3: hvac_mode = "cool";            action = "cooling" if heating else "idle"
    elif mode_byte == 4 and heat_pump:
                         hvac_mode = "heat";            action = "heating" if heating else "idle"
    elif mode_byte == 4 and not heat_pump:
                         hvac_mode = "emergency_heat";  action = "heating" if heating else "idle"
    elif mode_byte == 5: hvac_mode = "fan_only";        action = "fan"
    else:                hvac_mode = "off";             action = "off"

    return {
        "hvac_mode":    hvac_mode,
        "action":       action,
        "aux_heat":     hvac_mode == "emergency_heat" and heating,
        "current_temp": None,
        "setpoint":     setpoint_c,
        "fan_mode":     FAN_READ.get(fan_raw, "auto"),
        "heating":      heating,
    }


# ── Bridge MQTT ───────────────────────────────────────────────────────────────

class MoovairMQTTBridge:
    def __init__(self):
        self.cloud        = MoovairCloud()
        self.appliance_id = None
        self.device_info  = {}
        self._mqtt        = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                        client_id="moovair2mqtt", clean_session=True)
        self._cmd_queue   = asyncio.Queue()
        self._last_state  = {}
        self._running     = True
        self._loop        = None
        # ── Données FCM ──────────────────────────────────────────────────────
        self._fcm_temp         = None   # indoor temp entière (°C, FCM 67-byte[37])
        self._fcm_temp_precise = None   # indoor temp 0.5°C (FCM 88-byte A1[30]) — à confirmer
        self._fcm_outdoor_temp = None   # outdoor temp (FCM 88-byte A1[31]) — à confirmer
        self._fcm_humidity     = None   # indoor humidity % (FCM 67-byte[36]) — à confirmer
        self._fcm_temp_ts      = None   # monotonic timestamp dernière mise à jour FCM
        self._fcm_creds        = None
        self._fcm_client       = None
        self._user_id          = ""
        self._dry_mode         = "off"   # état dry mode local (optimiste)
        # ── Diagnostics bridge ────────────────────────────────────────────────
        self._diag_last_update    = None  # timestamp ISO dernière poll réussie
        self._diag_last_error     = ""    # texte dernière erreur
        self._diag_consecutive_errors = 0

    def _topic(self, suffix):
        return f"{CFG['mqtt_prefix']}/{self.appliance_id}/{suffix}"

    def _ha_discovery_topic(self, component, suffix="config"):
        uid = f"moovair_{self.appliance_id}"
        return f"homeassistant/{component}/{uid}/{suffix}"

    def _publish_discovery(self):
        """Publie les payloads MQTT Discovery pour Home Assistant."""
        dev = {
            "identifiers": [f"moovair_{self.appliance_id}"],
            "name":         "Moovair ST-1",
            "manufacturer": "Moovair / Midea",
            "model":        "ST-1 Zone Controller",
        }

        # ── Entité Climate ──────────────────────────────────────────────
        climate = {
            "name":                         "Moovair",
            "unique_id":                    f"moovair_{self.appliance_id}",
            "device":                       dev,
            "modes":                        ["off", "heat_cool", "heat", "cool", "fan_only", "emergency_heat"],
            "fan_modes":                    ["auto", "low", "medium", "high"],
            "current_temperature_topic":    self._topic("current_temperature"),
            "temperature_state_topic":      self._topic("target_temperature"),
            "mode_state_topic":             self._topic("mode"),
            "fan_mode_state_topic":         self._topic("fan_mode"),
            "action_topic":                 self._topic("action"),
            "temperature_command_topic":    self._topic("set/target_temperature"),
            "mode_command_topic":           self._topic("set/mode"),
            "fan_mode_command_topic":       self._topic("set/fan_mode"),
            "min_temp": 16, "max_temp": 30, "temp_step": 0.5,
            "temperature_unit":             "C",
            "availability_topic":           self._topic("availability"),
            "payload_available":            "online",
            "payload_not_available":        "offline",
        }
        self._mqtt.publish(
            self._ha_discovery_topic("climate"),
            json.dumps(climate), retain=True)

        # ── Aux Heat (résistif 10kW) ─────────────────────────────────────
        aux_heat = {
            "name":           "Aux Heat",
            "unique_id":      f"moovair_{self.appliance_id}_aux_heat",
            "device":         dev,
            "state_topic":    self._topic("aux_heat"),
            "payload_on":     "ON",
            "payload_off":    "OFF",
            "icon":           "mdi:lightning-bolt",
            "availability_topic": self._topic("availability"),
        }
        self._mqtt.publish(
            self._ha_discovery_topic("binary_sensor", "aux_heat/config"),
            json.dumps(aux_heat), retain=True)

        # ── Indoor Humidity ──────────────────────────────────────────────
        humidity = {
            "name":                 "Indoor Humidity",
            "unique_id":            f"moovair_{self.appliance_id}_indoor_humidity",
            "device":               dev,
            "state_topic":          self._topic("indoor_humidity"),
            "unit_of_measurement":  "%",
            "device_class":         "humidity",
            "state_class":          "measurement",
            "icon":                 "mdi:water-percent",
            "availability_topic":   self._topic("availability"),
        }
        self._mqtt.publish(
            self._ha_discovery_topic("sensor", "indoor_humidity/config"),
            json.dumps(humidity), retain=True)

        # ── Heat Pump Coil Temperature (T4, unité extérieure) ────────────
        outdoor_temp = {
            "name":                 "Heat Pump Coil Temperature",
            "unique_id":            f"moovair_{self.appliance_id}_outdoor_temp",
            "device":               dev,
            "state_topic":          self._topic("outdoor_temperature"),
            "unit_of_measurement":  "°C",
            "device_class":         "temperature",
            "state_class":          "measurement",
            "icon":                 "mdi:heat-pump",
            "availability_topic":   self._topic("availability"),
        }
        self._mqtt.publish(
            self._ha_discovery_topic("sensor", "outdoor_temperature/config"),
            json.dumps(outdoor_temp), retain=True)

        # ── Dry Mode select (minuterie, uniquement en cool ou auto) ─────────
        dry_mode = {
            "name":          "Dry Mode",
            "unique_id":     f"moovair_{self.appliance_id}_dry_mode",
            "device":        dev,
            "state_topic":   self._topic("dry_mode"),
            "command_topic": self._topic("set/dry_mode"),
            "options":       DRY_MODE_OPTIONS,
            "icon":          "mdi:air-humidifier-off",
            "availability": [
                {
                    "topic":                 self._topic("availability"),
                    "payload_available":     "online",
                    "payload_not_available": "offline",
                },
                {
                    "topic":                 self._topic("dry_mode_available"),
                    "payload_available":     "online",
                    "payload_not_available": "offline",
                },
            ],
            "availability_mode": "all",
        }
        self._mqtt.publish(
            self._ha_discovery_topic("select", "dry_mode/config"),
            json.dumps(dry_mode), retain=True)

        # ── Suppression des anciens sensors de diagnostic de HA ─────────────
        # Payload vide = supprime l'entité dans HA (si elle existait)
        for old in ("last_update/config", "last_error/config",
                    "consecutive_errors/config", "fcm_status/config"):
            self._mqtt.publish(
                self._ha_discovery_topic("sensor", old), "", retain=True)
        self._mqtt.publish(
            self._ha_discovery_topic("binary_sensor", "fcm_connected/config"),
            "", retain=True)
        # Note: les diagnostics sont toujours publiés sur diag/* (MQTT brut)
        # pour les développeurs, mais sans entités HA visibles.
        # État initial dry mode (offline = indisponible jusqu'au premier poll)
        self._mqtt.publish(self._topic("dry_mode"),           "off",     retain=True)
        self._mqtt.publish(self._topic("dry_mode_available"), "offline", retain=True)
        log.info("MQTT Discovery publiée (climate + aux_heat + humidity + coil temp + dry mode)")

    def _on_fcm_credentials_updated(self, creds):
        self._fcm_creds = creds

    def _on_fcm_message(self, message, persistent_id, obj):
        """Callback FCM — extrait température, humidité, outdoor temp des pushes Moovair."""
        try:
            parts = message.get('data', {}).get('message', '').split(';')
            if len(parts) < 3:
                return
            msg_type = parts[0].lower()
            if 'vital' not in msg_type and 'status' not in msg_type:
                return
            payload_json = json.loads(parts[2])
            payload = _fcm_decrypt_push(payload_json.get('msg', ''), self._user_id)
            if payload is None:
                return

            self._fcm_temp_ts = time.monotonic()
            # Publier un accusé de réception FCM visible dans HA
            if self.appliance_id:
                self._mqtt.publish(self._topic("diag/fcm_status"),
                                   f"message_received len={len(payload)}")
            changed = False

            # ── Payload 67 bytes (status condensé, toutes les ~11s) ──────────
            # byte[37] = indoor temp entière (°C) — confirmé en prod
            # byte[36] = indoor humidity (%) — à confirmer par l'utilisateur
            if len(payload) == 67:
                temp_c = payload[37]
                if temp_c != self._fcm_temp:
                    log.info("FCM indoor temp: %s°C", temp_c)
                    self._fcm_temp = temp_c
                    changed = True

                humidity = payload[36]
                if humidity != self._fcm_humidity and 0 < humidity <= 100:
                    log.info("FCM indoor humidity: %s%%", humidity)
                    self._fcm_humidity = humidity
                    changed = True

            # ── Payload 88 bytes, sous-type A1 (byte[17]==161) ───────────────
            # byte[30] = indoor temp haute précision (formule (byte-50)/2 → 0.5°C)
            # byte[31] = outdoor temp (même formule)
            # — À CONFIRMER par l'utilisateur via HA
            elif len(payload) == 88 and payload[17] == 161:
                raw_indoor  = payload[30] if len(payload) > 30 else 0
                raw_outdoor = payload[31] if len(payload) > 31 else 0
                if raw_indoor > 0:
                    temp_precise = (raw_indoor - 50) / 2.0
                    if temp_precise != self._fcm_temp_precise and -20 < temp_precise < 60:
                        log.info("FCM indoor temp precise: %.1f°C (raw=%d)", temp_precise, raw_indoor)
                        self._fcm_temp_precise = temp_precise
                        changed = True
                if raw_outdoor > 0:
                    temp_out = (raw_outdoor - 50) / 2.0
                    if temp_out != self._fcm_outdoor_temp and -40 < temp_out < 60:
                        log.info("FCM outdoor temp: %.1f°C (raw=%d)", temp_out, raw_outdoor)
                        self._fcm_outdoor_temp = temp_out
                        changed = True

            if changed and self._last_state and self._loop:
                self._loop.call_soon_threadsafe(self._publish_fcm_sensors)

        except Exception as e:
            log.debug("FCM message parse error: %s", e)

    def _publish_fcm_sensors(self):
        """Publie les données FCM sur MQTT (appelé depuis le thread FCM)."""
        if not self.appliance_id:
            return
        t = self._topic
        # Température : utiliser la précision 0.5°C si disponible, sinon entière
        temp_to_publish = self._fcm_temp_precise if self._fcm_temp_precise is not None else self._fcm_temp
        if temp_to_publish is not None:
            self._mqtt.publish(t("current_temperature"), str(temp_to_publish))
        if self._fcm_humidity is not None:
            self._mqtt.publish(t("indoor_humidity"), str(self._fcm_humidity))
        if self._fcm_outdoor_temp is not None:
            self._mqtt.publish(t("outdoor_temperature"), str(self._fcm_outdoor_temp))
        # Statut FCM
        self._mqtt.publish(t("diag/fcm_connected"), "ON")

    async def _fcm_register_token(self, fcm_token):
        """Enregistre le token FCM auprès du cloud Moovair (utilise la session courante)."""
        body = {"format": "2", "stamp": _ts(), "language": "en_US", "src": SRC,
                "sessionId": self.cloud.session_id, "pushToken": fcm_token, "pushType": "5"}
        body["sign"] = _sign("/v1/user/push/token/update", body)
        await self.cloud._client.post(f"{BASE_URL}/v1/user/push/token/update", data=body)
        log.debug("FCM token enregistré avec session %s…", self.cloud.session_id[:8])

    def _fcm_pub_status(self, status: str):
        """Publie le statut FCM sur MQTT pour diagnostic sans log."""
        if self.appliance_id:
            self._mqtt.publish(self._topic("diag/fcm_status"), status)
            log.info("FCM status: %s", status)

    async def _fcm_loop(self):
        """Boucle FCM — démarre le listener et reconnecte automatiquement en cas de coupure."""
        retry_delay = 30
        attempt = 0
        while self._running:
            attempt += 1
            try:
                self._fcm_pub_status(f"connecting (attempt {attempt})")
                fcm_config = FcmRegisterConfig(
                    project_id=FCM_PROJECT_ID, app_id=FCM_APP_ID,
                    api_key=FCM_API_KEY, messaging_sender_id=FCM_SENDER_ID,
                )
                self._fcm_client = FcmPushClient(
                    callback=self._on_fcm_message,
                    fcm_config=fcm_config,
                    credentials=self._fcm_creds,
                    credentials_updated_callback=self._on_fcm_credentials_updated,
                )
                fcm_token = await self._fcm_client.checkin_or_register()
                self._fcm_pub_status("token_obtained")
                await self._fcm_register_token(fcm_token)
                self._fcm_pub_status("token_registered")

                await self._fcm_client.start()
                self._fcm_pub_status("listening")
                log.info("FCM listener démarré — température ambiante live activée")
                retry_delay = 30

                # Surveiller la connexion FCM jusqu'à ce qu'elle tombe
                while self._running:
                    await asyncio.sleep(60)
                    if not self._fcm_client.is_started():
                        self._fcm_pub_status("dropped — reconnecting")
                        log.warning("FCM client arrêté, reconnexion...")
                        break
                    if (self._fcm_temp_ts is not None and
                            time.monotonic() - self._fcm_temp_ts > FCM_MAX_SILENCE):
                        self._fcm_pub_status(f"silent >{FCM_MAX_SILENCE}s — reconnecting")
                        log.warning("Pas de push FCM depuis >%ds, reconnexion...", FCM_MAX_SILENCE)
                        await self._fcm_client.stop()
                        break

            except Exception as e:
                msg = str(e)[:120]
                self._fcm_pub_status(f"error: {msg}")
                log.warning("FCM échec (%s), retry dans %ds", e, retry_delay)

            if not self._running:
                break
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    def _publish_state(self, state):
        """Publie l'état courant sur les topics MQTT."""
        # Température : priorité à la précision 0.5°C (A1), fallback entière (67-byte)
        fcm_fresh = (self._fcm_temp_ts is not None and
                     time.monotonic() - self._fcm_temp_ts < FCM_TEMP_MAX_AGE)
        if fcm_fresh:
            state = dict(state)
            temp = self._fcm_temp_precise if self._fcm_temp_precise is not None else self._fcm_temp
            if temp is not None:
                state["current_temp"] = temp

        if state.get("current_temp") is None:
            state = dict(state)
            state.pop("current_temp", None)

        changed = state != self._last_state
        if not changed:
            return

        t = self._topic
        if state.get("current_temp") is not None:
            self._mqtt.publish(t("current_temperature"), str(state["current_temp"]))
        self._mqtt.publish(t("target_temperature"),  str(state["setpoint"]))
        self._mqtt.publish(t("mode"),                state["hvac_mode"])
        self._mqtt.publish(t("fan_mode"),            state["fan_mode"])
        self._mqtt.publish(t("action"),              state["action"])
        self._mqtt.publish(t("aux_heat"),            "ON" if state["aux_heat"] else "OFF")
        self._mqtt.publish(t("availability"),        "online")

        # Diagnostics bridge
        now_iso = datetime.now(timezone.utc).isoformat()
        self._mqtt.publish(t("diag/last_update"),          now_iso)
        self._mqtt.publish(t("diag/consecutive_errors"),   str(self._diag_consecutive_errors))
        fcm_ok = fcm_fresh and (self._fcm_client is not None and self._fcm_client.is_started())
        self._mqtt.publish(t("diag/fcm_connected"),        "ON" if fcm_ok else "OFF")
        if self._diag_last_error:
            self._mqtt.publish(t("diag/last_error"),       self._diag_last_error)

        # Sensors FCM (si disponibles)
        if fcm_fresh:
            if self._fcm_humidity is not None:
                self._mqtt.publish(t("indoor_humidity"),   str(self._fcm_humidity))
            if self._fcm_outdoor_temp is not None:
                self._mqtt.publish(t("outdoor_temperature"), str(self._fcm_outdoor_temp))

        if self._last_state:
            changes = {k: v for k, v in state.items() if v != self._last_state.get(k)}
            if changes:
                log.info("État mis à jour: %s", changes)
        else:
            log.info("Premier état publié: %s", state)

        self._last_state = state.copy()

        # Dry mode : disponibilité selon le mode HVAC courant
        dry_ok = state["hvac_mode"] in DRY_MODE_REQUIRES
        self._mqtt.publish(self._topic("dry_mode_available"),
                           "online" if dry_ok else "offline")
        if not dry_ok and self._dry_mode != "off":
            self._dry_mode = "off"
            self._mqtt.publish(self._topic("dry_mode"), "off")
        else:
            self._mqtt.publish(self._topic("dry_mode"), self._dry_mode)

    def _on_mqtt_message(self, client, userdata, msg):
        """Callback MQTT — reçoit les commandes de Home Assistant."""
        topic   = msg.topic
        payload = msg.payload.decode().strip()
        prefix  = self._topic("set/")

        if not topic.startswith(prefix):
            return

        cmd = topic[len(prefix):]
        log.debug("Commande MQTT reçue: %s = %s", cmd, payload)

        # ── Commande dry mode (gérée séparément, pas via cmd_queue) ──────────
        if cmd == "dry_mode":
            if payload not in DRY_MODE_OPTIONS:
                log.warning("Dry mode invalide: %s", payload)
                return
            current_mode = self._last_state.get("hvac_mode", "off")
            if payload != "off" and current_mode not in DRY_MODE_REQUIRES:
                log.warning("Dry mode ignoré — mode HVAC actuel: %s", current_mode)
                return
            if self._loop:
                self._loop.call_soon_threadsafe(
                    self._cmd_queue.put_nowait, {"dry_mode": payload})
            return

        current = self._last_state.copy()
        if cmd == "mode":
            current["hvac_mode"] = payload
        elif cmd == "target_temperature":
            try:
                current["setpoint"] = float(payload)
            except ValueError:
                log.warning("Setpoint invalide: %s", payload)
                return
        elif cmd == "fan_mode":
            current["fan_mode"] = payload
        else:
            log.warning("Commande inconnue: %s", cmd)
            return

        if self._loop:
            self._loop.call_soon_threadsafe(self._cmd_queue.put_nowait, current)
        else:
            log.warning("Loop pas encore prête, commande ignorée")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connecté à %s:%s", CFG["mqtt_host"], CFG["mqtt_port"])
            # Re-abonner aux topics de commande à chaque reconnexion
            prefix = self._topic("set/")
            for cmd in ("mode", "target_temperature", "fan_mode"):
                self._mqtt.subscribe(f"{prefix}{cmd}")
                log.debug("Abonné: %s%s", prefix, cmd)
            # Re-publier "online" après reconnexion (annule le LWT "offline")
            self._mqtt.publish(self._topic("availability"), "online", retain=True)
        else:
            log.error("Connexion MQTT échouée: rc=%s", rc)

    def _on_mqtt_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("Déconnexion MQTT inattendue (rc=%s) — reconnexion auto", rc)

    async def _handle_command(self, cmd_state):
        """Exécute une commande de contrôle reçue de HA."""

        # ── Commande dry mode ────────────────────────────────────────────────
        if "dry_mode" in cmd_state:
            option   = cmd_state["dry_mode"]
            duration = DRY_MODE_DURATIONS.get(option, 0)
            log.info("Dry mode: %s (%d min)", option, duration)
            try:
                await self.cloud.send_dry_mode(self.appliance_id, duration)
                self._dry_mode = option
                self._mqtt.publish(self._topic("dry_mode"), option)
                log.info("Dry mode appliqué: %s", option)
            except Exception as e:
                log.error("Erreur dry mode: %s", e)
            return

        log.info("Exécution commande: mode=%s setpoint=%s fan=%s",
                 cmd_state.get("hvac_mode"),
                 cmd_state.get("setpoint"),
                 cmd_state.get("fan_mode"))
        try:
            # 1) Mise à jour OPTIMISTE: publier la commande immédiatement dans HA
            #    sans attendre la confirmation du device. HA met à jour instantanément.
            optimistic = self._last_state.copy()
            optimistic.update({
                "hvac_mode": cmd_state["hvac_mode"],
                "setpoint":  cmd_state["setpoint"],
                "fan_mode":  cmd_state["fan_mode"],
                "action":    "off" if cmd_state["hvac_mode"] == "off" else optimistic.get("action", "idle"),
            })
            self._last_state = {}           # forcer la publication même si identique
            self._publish_state(optimistic)

            # 2) Envoyer la vraie commande au cloud
            await self.cloud.send_control(
                self.appliance_id,
                setpoint_c = cmd_state["setpoint"],
                hvac_mode  = cmd_state["hvac_mode"],
                fan_mode   = cmd_state["fan_mode"],
            )

            # 3) Attendre que le device applique, puis lire l'état réel
            await asyncio.sleep(5)
            self._last_state = {}           # forcer re-publication de l'état réel
            state = await self.cloud.read_state(self.appliance_id)
            self._publish_state(state)

        except Exception as e:
            log.error("Erreur commande: %s", e)
            if "session" in str(e).lower() or "invalid" in str(e).lower():
                await self._relogin()

    async def _relogin(self):
        """Re-login en cas d'expiration de session."""
        log.info("Re-login en cours...")
        try:
            await self.cloud.login()
            # Re-enregistrer le token FCM avec la nouvelle session
            if self._fcm_client is not None:
                try:
                    fcm_token = await self._fcm_client.checkin_or_register()
                    await self._fcm_register_token(fcm_token)
                    log.info("FCM token re-enregistré après re-login")
                except Exception as fe:
                    log.warning("Re-enregistrement FCM échoué: %s", fe)
        except Exception as e:
            log.error("Re-login échoué: %s", e)
            self._mqtt.publish(self._topic("availability"), "offline")

    async def run(self):
        """Boucle principale du bridge."""
        # Capturer le loop asyncio pour les callbacks paho-mqtt (thread séparé)
        self._loop = asyncio.get_running_loop()

        # Login cloud
        await self.cloud.login()
        self.appliance_id, self.device_info = await self.cloud.get_appliance_id()
        self._user_id = self.cloud.user_id

        # Démarrer la boucle FCM (température ambiante live, reconnexion automatique)
        asyncio.ensure_future(self._fcm_loop())

        # Connexion MQTT
        self._mqtt.on_connect    = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message    = self._on_mqtt_message
        if CFG["mqtt_user"]:
            self._mqtt.username_pw_set(CFG["mqtt_user"], CFG["mqtt_pass"])
        self._mqtt.will_set(self._topic("availability"), "offline", retain=True)
        self._mqtt.reconnect_delay_set(min_delay=5, max_delay=30)
        self._mqtt.connect(CFG["mqtt_host"], CFG["mqtt_port"], keepalive=60)
        self._mqtt.loop_start()

        await asyncio.sleep(2)  # Laisser MQTT se connecter
        self._publish_discovery()

        log.info("Bridge démarré — poll toutes les %ss", CFG["poll_interval"])
        poll_interval = CFG["poll_interval"]
        last_poll     = 0

        while self._running:
            # Traiter les commandes en attente
            while not self._cmd_queue.empty():
                cmd = await self._cmd_queue.get()
                await self._handle_command(cmd)
                last_poll = 0

            # Poll périodique
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                try:
                    state = await self.cloud.read_state(self.appliance_id)
                    self._publish_state(state)
                    self._mqtt.publish(self._topic("availability"), "online", retain=True)
                    self._diag_consecutive_errors = 0
                    last_poll = now
                except Exception as e:
                    self._diag_consecutive_errors += 1
                    self._diag_last_error = str(e)[:200]
                    log.error("Erreur lecture état (%s/3): %s", self._diag_consecutive_errors, e)
                    last_poll = now
                    self._mqtt.publish(self._topic("diag/consecutive_errors"),
                                       str(self._diag_consecutive_errors))
                    self._mqtt.publish(self._topic("diag/last_error"), self._diag_last_error)
                    if self._diag_consecutive_errors >= 3:
                        log.warning("3 erreurs consécutives — re-login...")
                        await self._relogin()
                        self._diag_consecutive_errors = 0

            await asyncio.sleep(1)

        # Arrêt propre
        log.info("Arrêt du bridge...")
        if self._fcm_client:
            await self._fcm_client.stop()
        self._mqtt.publish(self._topic("availability"), "offline")
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
        await self.cloud.close()


# ── Point d'entrée ────────────────────────────────────────────────────────────

async def _main():
    bridge = MoovairMQTTBridge()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(bridge, '_running', False))

    while bridge._running:
        try:
            await bridge.run()
        except Exception as e:
            log.critical("Erreur fatale inattendue: %s — redémarrage dans 15s", e, exc_info=True)
            if bridge._running:
                await asyncio.sleep(15)
                bridge = MoovairMQTTBridge()  # reset complet


if __name__ == "__main__":
    log.info("moovair2mqtt — démarrage")
    asyncio.run(_main())
