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
FCM_SENDER_ID  = "1016425309209"
FCM_APP_ID     = "1:1016425309209:android:a397210f6f94bc2d7f3688"
FCM_PROJECT_ID = "moovair"
FCM_API_KEY    = "AIzaSyA7HhxqQhZ5zfc1euu-HK6B3CZfAtNjrek"

FAN_READ  = {30: "low", 60: "medium", 90: "high", 102: "auto"}
FAN_WRITE = {"low": 30, "medium": 60, "high": 90, "auto": 102}
MODE_WRITE = {"heat_cool": 1, "cool": 2, "dry": 3, "heat": 4,
              "emergency_heat": 4, "fan_only": 5}

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
    elif mode_byte == 1: hvac_mode = "heat_cool";  action = "heating" if heating else "idle"
    elif mode_byte == 2: hvac_mode = "cool";        action = "cooling" if heating else "idle"
    elif mode_byte == 3: hvac_mode = "dry";         action = "drying"  if heating else "idle"
    elif mode_byte == 4: hvac_mode = "heat";        action = "heating" if heating else "idle"
    elif mode_byte == 5: hvac_mode = "fan_only";    action = "fan"
    else:                hvac_mode = "off";         action = "off"

    return {
        "hvac_mode":    hvac_mode,
        "action":       action,
        "aux_heat":     power == 1 and mode_byte == 4 and not heat_pump,
        "current_temp": None,   # sera mis à jour par le FCM listener
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
        self._loop        = None   # sera initialisé dans run()
        self._fcm_temp    = None   # température ambiante reçue via FCM push
        self._fcm_client  = None
        self._user_id     = ""

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
            "modes":                        ["off", "heat", "cool", "heat_cool", "fan_only", "dry"],
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

        # ── Sensor Aux Heat (élément résistif 10kW) ─────────────────────
        aux_heat = {
            "name":           "Moovair Aux-Heat",
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

        log.info("MQTT Discovery publiée (climate + aux_heat sensor)")

    def _on_fcm_message(self, message, persistent_id, obj):
        """Callback FCM — extrait la température ambiante du push Moovair."""
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
            # Message 67 bytes : byte[37] = température ambiante en °C
            if len(payload) == 67:
                temp_c = payload[37]
                if temp_c != self._fcm_temp:
                    log.info("Température FCM mise à jour: %s°C", temp_c)
                    self._fcm_temp = temp_c
                    # Forcer re-publication si l'état existe déjà
                    if self._last_state and self._loop:
                        self._loop.call_soon_threadsafe(self._publish_fcm_temp)
        except Exception as e:
            log.debug("FCM message parse error: %s", e)

    def _publish_fcm_temp(self):
        """Publie uniquement la température FCM sur MQTT (appelé depuis le thread FCM)."""
        if self._fcm_temp is not None and self.appliance_id:
            self._mqtt.publish(self._topic("current_temperature"), str(self._fcm_temp))
            log.debug("FCM temp publiée: %s°C", self._fcm_temp)

    async def _start_fcm(self, session_id):
        """Démarre le listener FCM pour recevoir la température ambiante en temps réel."""
        try:
            fcm_config = FcmRegisterConfig(
                project_id=FCM_PROJECT_ID, app_id=FCM_APP_ID,
                api_key=FCM_API_KEY, messaging_sender_id=FCM_SENDER_ID,
            )
            self._fcm_client = FcmPushClient(callback=self._on_fcm_message, fcm_config=fcm_config)
            fcm_token = await self._fcm_client.checkin_or_register()

            # Enregistrer le token FCM avec le cloud Midea
            body = {"format": "2", "stamp": _ts(), "language": "en_US", "src": SRC,
                    "sessionId": session_id, "pushToken": fcm_token, "pushType": "5"}
            body["sign"] = _sign("/v1/user/push/token/update", body)
            await self.cloud._client.post(f"{BASE_URL}/v1/user/push/token/update", data=body)

            await self._fcm_client.start()
            log.info("FCM listener démarré — température ambiante live activée")
        except Exception as e:
            log.warning("FCM listener échec: %s", e)

    def _publish_state(self, state):
        """Publie l'état courant sur les topics MQTT."""
        # Utiliser la température FCM si disponible
        if self._fcm_temp is not None:
            state = dict(state)
            state["current_temp"] = self._fcm_temp

        if state.get("current_temp") is None:
            state = dict(state)
            state.pop("current_temp", None)  # ne pas publier si on n'a pas encore de temp

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

        if self._last_state:
            changes = {k: v for k, v in state.items() if v != self._last_state.get(k)}
            if changes:
                log.info("État mis à jour: %s", changes)
        else:
            log.info("Premier état publié: %s", state)

        self._last_state = state.copy()

    def _on_mqtt_message(self, client, userdata, msg):
        """Callback MQTT — reçoit les commandes de Home Assistant."""
        topic   = msg.topic
        payload = msg.payload.decode().strip()
        prefix  = self._topic("set/")

        if not topic.startswith(prefix):
            return

        cmd = topic[len(prefix):]
        log.debug("Commande MQTT reçue: %s = %s", cmd, payload)

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

        # Démarrer le listener FCM (température ambiante live)
        asyncio.ensure_future(self._start_fcm(self.cloud.session_id))

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
        consecutive_errors = 0

        while self._running:
            # Traiter les commandes en attente
            while not self._cmd_queue.empty():
                cmd = await self._cmd_queue.get()
                await self._handle_command(cmd)
                last_poll = 0  # Forcer re-poll après commande

            # Poll périodique
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                try:
                    state = await self.cloud.read_state(self.appliance_id)
                    self._publish_state(state)
                    self._mqtt.publish(self._topic("availability"), "online", retain=True)
                    consecutive_errors = 0
                    last_poll = now
                except Exception as e:
                    consecutive_errors += 1
                    log.error("Erreur lecture état (%s/3): %s", consecutive_errors, e)
                    last_poll = now  # éviter de spam en boucle
                    if consecutive_errors >= 3:
                        log.warning("3 erreurs consécutives — re-login...")
                        await self._relogin()
                        consecutive_errors = 0

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
