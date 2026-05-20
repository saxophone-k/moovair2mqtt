# moovair2mqtt

MQTT bridge for the **Moovair ST-1** central heat pump thermostat, enabling full control and monitoring from **Home Assistant**.

Follows the same conventions as [mysa2mqtt](https://github.com/bourquep/mysa2mqtt).

> **Disclaimer:** This project was built through reverse engineering of the Moovair Android APK. It is not affiliated with or endorsed by Moovair or Midea.
>
> ⚠️ **Stability warning:** This bridge relies entirely on Moovair's **undocumented private cloud API**. If Moovair changes their API, authentication system, or encryption at any time, this bridge may stop working with no warning. Use it knowing this risk.
>
> **A note from the author:** I am not a programmer — this entire project was built with the help of AI-assisted development. If you run into issues or have questions, I'll do my best to help, but please keep in mind that my ability to debug code is very limited. That said, feel free to open an issue — maybe someone in the community can step in! 😄

---

## Features

- ✅ **Ambient temperature** with **0.5°C precision** (via FCM push from thermostat)
- ✅ **Indoor humidity** sensor (from thermostat's built-in sensor)
- ✅ **Heat pump coil temperature** (T4 sensor on outdoor unit — useful for diagnostics)
- ✅ Full control: setpoint, HVAC mode, fan speed
- ✅ **Modes:** Auto (heat/cool), Heat, Cool, Fan Only, **Emergency Heat**
- ⚠️ **Aux Heat** sensor — needs more reverse engineering. Is not currently working reliably
- ✅ **Dry Mode** — dehumidification with 30-minute timer and real-time countdown (see notes below)
- ✅ Automatic MQTT Discovery (Home Assistant auto-detects device, zero config)
- ✅ Automatic session re-login when session expires
- ✅ FCM push for real-time temperature & humidity updates (sub-second latency)
- ⚠️ Aux-heat detection in normal heat mode (supplemental PTC): requires winter testing at -10°C or below (TODO)

---

## Requirements

- **Docker** (to run the bridge as a container)
- **An MQTT broker** — e.g. [Mosquitto](https://mosquitto.org/)
- **Home Assistant** with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) enabled
- **A Moovair account** (email + password from the Moovair app)

> ⚠️ **Known limitation — session conflict:** The Moovair cloud only supports **one active session per account**. The bridge and the Moovair mobile app cannot run at the same time — the bridge takes over the session and the app gets disconnected. Close the app when the bridge is running.

---

## Quick Start (Docker)

### docker-compose.yml

```yaml
services:
  moovair2mqtt:
    image: ghcr.io/saxophone-k/moovair2mqtt:latest
    restart: unless-stopped
    environment:
      M2M_MOOVAIR_USERNAME: "your.email@example.com"
      M2M_MOOVAIR_PASSWORD: "your_password"
      M2M_MQTT_HOST: "192.168.1.x"        # Your MQTT broker IP
      M2M_MQTT_PORT: "1883"
      # M2M_MQTT_USERNAME: ""             # if your broker requires auth
      # M2M_MQTT_PASSWORD: ""
      M2M_POLL_INTERVAL: "30"             # seconds between polls (recommended: 30)
      M2M_LOG_LEVEL: "info"               # debug, info, warning, error
```

### Start

```bash
docker compose up -d
```

### Home Assistant

Home Assistant auto-discovers the thermostat via MQTT Discovery. Check:  
**Settings → Devices & Services → MQTT**

---

## TrueNAS Scale

1. **Apps → Custom App**
2. Image: `ghcr.io/saxophone-k/moovair2mqtt:latest`
3. Add environment variables (see table below)
4. Deploy

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `M2M_MOOVAIR_USERNAME` | ✅ | — | Moovair account email |
| `M2M_MOOVAIR_PASSWORD` | ✅ | — | Moovair account password |
| `M2M_MQTT_HOST` | ✅ | — | MQTT broker IP or hostname |
| `M2M_MQTT_PORT` | — | `1883` | MQTT broker port |
| `M2M_MQTT_USERNAME` | — | — | MQTT username (if auth enabled) |
| `M2M_MQTT_PASSWORD` | — | — | MQTT password (if auth enabled) |
| `M2M_MQTT_TOPIC_PREFIX` | — | `moovair2mqtt` | MQTT topic prefix |
| `M2M_POLL_INTERVAL` | — | `30` | Poll interval in seconds |
| `M2M_LOG_LEVEL` | — | `info` | Log level: `debug` / `info` / `warning` / `error` |

---

## Home Assistant Entities

| Entity | Type | Description |
|--------|------|-------------|
| Moovair | `climate` | Full thermostat control (mode, setpoint, fan) |
| Aux Heat | `binary_sensor` | Electric backup element active (Emergency Heat mode) |
| Indoor Humidity | `sensor` | Relative humidity % from thermostat sensor |
| Heat Pump Coil Temperature | `sensor` | T4 outdoor unit coil temp (°C) — NOT outdoor ambient |
| Dry Mode | `switch` | Dehumidification ON/OFF toggle (see notes) |
| Dry Mode Remaining | `sensor` | Minutes remaining in current dry mode session |

---

## Dry Mode

Dry mode activates the dehumidification function of the ST-1, which reduces humidity without aggressively cooling the room.

### How it works in the Moovair app

The Moovair app offers a dry mode with 4 duration options (15 / 30 / 45 / 60 minutes). The thermostat dehumidifies for the selected duration, then automatically returns to the previous mode.

### Current implementation in this bridge

**Only 30-minute sessions are supported.** The bridge exposes a simple `ON/OFF` select entity in Home Assistant.

When the switch is turned **ON**:
1. The bridge sends the dry mode command to the device
2. The thermostat activates dry mode for 30 minutes
3. **Dry Mode Remaining** counts down in real time using the thermostat's own timer (live FCM push data — not a bridge-side estimate)
4. When the timer reaches 0, the switch automatically turns **OFF**

You can also turn the switch **OFF** at any time to cancel dry mode immediately.

**Why only 30 minutes?** The Moovair cloud API provides two separate commands for dry mode: one to set the duration (`dry_time_interval`), and one to activate the mode. Through reverse engineering, we were able to implement the activation reliably. However, setting a custom duration via the cloud API does not appear to take effect when sent through the transparent-send protocol path used by this bridge — the device always uses its last locally-set duration.

**Contributions welcome:** If you can figure out how to reliably set the dry mode duration (15 / 45 / 60 min) via the cloud API, please open a PR! The relevant code is in `MoovairCloud.send_dry_mode()`.

---

## MQTT Topics

Topics follow the format `{prefix}/{device_id}/{field}` (default prefix: `moovair2mqtt`).

### State (bridge → HA)

| Topic | Values | Notes |
|-------|--------|-------|
| `.../current_temperature` | e.g. `22.5` | 0.5°C precision from FCM |
| `.../target_temperature` | e.g. `23.0` | |
| `.../mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `emergency_heat` | |
| `.../fan_mode` | `auto`, `low`, `medium`, `high` | |
| `.../action` | `heating`, `cooling`, `idle`, `off`, `fan` | |
| `.../aux_heat` | `ON`, `OFF` | |
| `.../indoor_humidity` | e.g. `53` | % |
| `.../outdoor_temperature` | e.g. `29.5` | T4 coil sensor, not ambient |
| `.../dry_mode` | `ON`, `OFF` | |
| `.../dry_mode_remaining` | `0`–`30` | Minutes, real thermostat data |
| `.../availability` | `online`, `offline` | LWT |

### Commands (HA → bridge)

| Topic | Values |
|-------|--------|
| `.../set/mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `emergency_heat` |
| `.../set/target_temperature` | e.g. `22.0` |
| `.../set/fan_mode` | `auto`, `low`, `medium`, `high` |
| `.../set/dry_mode` | `ON`, `OFF` |

---

## How It Works

This bridge communicates with the Moovair cloud (Midea NetHomePlus infrastructure) using a reverse-engineered protocol:

1. **Authentication:** SHA256 signature + AES/ECB session key derived from login
2. **State polling:** `app2base/data/transmit` → Lua `json2data` → `appliance/transparent/send` → decoded binary payload
3. **Control:** Same path with a control JSON payload
4. **Live temperature & humidity:** Firebase Cloud Messaging (FCM) — the thermostat pushes state updates every ~11 seconds; `byte[37]` = indoor temp, `byte[36]` = humidity %, `byte[57]` = dry mode remaining minutes
5. **Precise temperature (0.5°C):** FCM sub-type A1 message, `byte[30]` using formula `(raw - 50) / 2`

### Emergency Heat vs. Heat

- **Heat mode:** Uses the heat pump. The resistive electric element (if your furnace is equipped with one) supplements automatically. Automatic detection of supplemental PTC and conditions for supplemental PTC activation in this mode have not yet been resverse engineered.
- **Emergency Heat:** Bypasses the heat pump entirely. Only the electric element runs (if equipped). Use only if the heat pump is broken. A warning is displayed on the thermostat.

---

## Contributing

Pull requests are welcome!

Areas where community help is especially needed:

- **Dry mode duration control** — setting 15 / 45 / 60 min via the cloud API (see `send_dry_mode()`)
- **Supplemental PTC detection in normal heat mode** — requires testing to identify which payload byte changes when the electric element activates
- **Additional entities** — swing, eco mode, turbo, sleep mode (APK analysis suggests these exist in the protocol but haven't been implemented)

To report a bug or suggest an improvement: [open an issue](../../issues).

---

## License

MIT — see [LICENSE](LICENSE)
