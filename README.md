# moovair2mqtt

MQTT bridge for the **Moovair ST-1** central heat pump thermostat, enabling full control and monitoring from **Home Assistant**.

Follows the same conventions as [mysa2mqtt](https://github.com/bourquep/mysa2mqtt).

> **Disclaimer:** This project was built through reverse engineering of the Moovair Android APK. It is not affiliated with or endorsed by Moovair or Midea.
>
> ⚠️ **Stability warning:** This bridge relies entirely on Moovair's **undocumented private cloud API**. If Moovair changes their API, authentication system, or encryption at any time, this bridge may stop working with no warning. Use it knowing this risk.
>
> **A note from the author:** I am not a programmer — this entire project was vibe-coded with AI assistance. If you run into issues or have questions, I'll do my best to help, but please keep in mind that my ability to debug code is very limited. That said, feel free to open an issue — maybe someone in the community can step in! 😄

---

## Features

- ✅ **Ambient temperature** with **0.5°C precision** (via FCM push from thermostat)
- ✅ **Indoor humidity** sensor (from thermostat's built-in sensor)
- ✅ **Outdoor Coil Temperature** (T4 sensor on outdoor unit — useful for diagnostics)
- ✅ Full control: setpoint, HVAC mode, fan speed
- ✅ **Modes:** Auto (heat/cool), Heat, Cool, Fan Only, Emergency Heat
- ✅ **Heat Pump** sensor — shows when the outdoor unit compressor is running
- ✅ **Aux Heat** sensor — shows when the 10kW electric backup element is physically active
- ✅ **Dry Mode** — dehumidification with 30-minute timer and real-time countdown (see notes below)
- ✅ Automatic MQTT Discovery (Home Assistant auto-detects device, zero config)
- ✅ Automatic session re-login when session expires
- ✅ FCM push for real-time temperature & humidity updates (~11s latency)

### Known limitations

- ⚠️ **Emergency Heat cannot be activated from HA** — the Moovair cloud API path used by this bridge does not support changing the PTC mode via the binary protocol. Emergency Heat must be set on the physical thermostat. HA correctly reflects the state when it is active.
- ⚠️ **Aux Heat in normal Heat mode** — detection of the supplemental PTC element in normal heat mode (not Emergency Heat) requires winter testing at outdoor temperatures below -10°C. Aux Heat currently only activates in Emergency Heat mode.
- ⚠️ **Dry Mode duration** — only 30-minute sessions are supported (see notes below).

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
| Aux Heat | `binary_sensor` | 10kW electric element physically active (`mdi:heating-coil`) |
| Heat Pump | `binary_sensor` | Outdoor unit compressor running (`mdi:heat-pump`) |
| Indoor Humidity | `sensor` | Relative humidity % from thermostat sensor |
| Outdoor Coil Temperature | `sensor` | T4 outdoor unit coil temp (°C) — NOT outdoor ambient |
| Dry Mode | `switch` | Dehumidification ON/OFF toggle (see notes) |
| Dry Mode Remaining | `sensor` | Minutes remaining in current dry mode session |

---

## Sensors explained

### Aux Heat vs Heat Pump

These two sensors complement each other:

| Sensor | Indicates | When ON |
|--------|-----------|---------|
| **Heat Pump** | Outdoor compressor running | System actively heating or cooling via heat pump |
| **Aux Heat** | 10kW resistive element drawing power | Emergency Heat mode is active AND element is running |

In **normal Heat mode**: Heat Pump = ON (compressor), Aux Heat = OFF  
In **Emergency Heat mode**: Heat Pump = OFF (bypassed), Aux Heat = ON (element running)  
In **Cool mode**: Heat Pump = ON (compressor cooling), Aux Heat = OFF

> **Note:** Detection of the supplemental PTC element in normal Heat mode (the automatic electric assist when it's very cold outside) requires winter testing at -10°C or below. This is a known TODO.

### Outdoor Coil Temperature

This is the **T4 sensor** on the outdoor heat exchanger — **not** the outdoor ambient air temperature. The Moovair app displays outdoor weather data from AccuWeather, which is a different value.

- In **Cool mode**: the coil is the condenser and will be warmer than ambient (rejecting heat)
- In **Heat mode**: the coil is the evaporator and will be cooler than ambient (absorbing heat)

Useful for diagnosing system performance and refrigerant issues.

---

## Dry Mode

Dry mode activates the dehumidification function of the ST-1, which cycles the compressor on/off at reduced capacity to remove moisture.

### Current implementation

**Only 30-minute sessions are supported.** Toggle the switch ON to start a 30-minute session.

When the switch is turned **ON**:
1. The bridge sends the dry mode command to the device
2. The thermostat activates dry mode for 30 minutes
3. **Dry Mode Remaining** counts down in real time (live FCM data from thermostat, not a bridge estimate)
4. When the timer reaches 0, the switch automatically turns **OFF**

You can also turn the switch **OFF** at any time to cancel immediately.

**Why only 30 minutes?** The Moovair cloud API provides two separate commands for dry mode: one to set the duration (`dry_time_interval`) and one to activate. Through reverse engineering, the activation is reliable, but setting a custom duration via the transparent-send protocol path does not take effect — the device uses its last locally-set duration.

**Contributions welcome:** If you can figure out how to reliably set the dry mode duration (15 / 45 / 60 min), please open a PR — see `MoovairCloud.send_dry_mode()`.

---

## MQTT Topics

Topics follow the format `{prefix}/{device_id}/{field}` (default prefix: `moovair2mqtt`).

### State (bridge → HA)

| Topic | Values | Notes |
|-------|--------|-------|
| `.../current_temperature` | e.g. `22.5` | 0.5°C precision from FCM |
| `.../target_temperature` | e.g. `23.0` | |
| `.../mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only` | |
| `.../fan_mode` | `auto`, `low`, `medium`, `high` | |
| `.../action` | `heating`, `cooling`, `idle`, `off`, `fan` | |
| `.../aux_heat` | `ON`, `OFF` | PTC element physically running |
| `.../heat_pump` | `ON`, `OFF` | Compressor running |
| `.../indoor_humidity` | e.g. `53` | % |
| `.../outdoor_temperature` | e.g. `29.5` | T4 coil sensor, not ambient |
| `.../dry_mode` | `ON`, `OFF` | |
| `.../dry_mode_remaining` | `0`–`30` | Minutes, real thermostat data |
| `.../availability` | `online`, `offline` | LWT |

### Commands (HA → bridge)

| Topic | Values |
|-------|--------|
| `.../set/mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only` |
| `.../set/target_temperature` | e.g. `22.0` |
| `.../set/fan_mode` | `auto`, `low`, `medium`, `high` |
| `.../set/dry_mode` | `ON`, `OFF` |

---

## How It Works

This bridge communicates with the Moovair cloud (Midea NetHomePlus infrastructure) using a reverse-engineered protocol:

1. **Authentication:** SHA256 signature + AES/ECB session key derived from login
2. **State polling:** `app2base/data/transmit` → Lua `json2data` → `appliance/transparent/send` → decoded binary payload
3. **Control:** Same path with a control JSON payload
4. **Live temperature & humidity:** Firebase Cloud Messaging (FCM) — the thermostat pushes state updates every ~11 seconds
5. **Precise temperature (0.5°C):** FCM sub-type A1 message, `byte[30]`, formula `(raw - 50) / 2`
6. **Humidity:** FCM condensed status, `byte[36]`, direct %
7. **Aux Heat:** HTTP payload `byte[40]` bit 2 — empirically confirmed via physical thermostat testing
8. **Heat Pump:** HTTP payload `byte[85]` — compressor run status

### Emergency Heat

Emergency Heat mode (PTC-only, heat pump bypassed) is **detected** correctly via `payload[18]` (0 = no heat pump, 8 = heat pump active) and `payload[40]` bit2 (element running). However, it **cannot be activated from HA** — the binary protocol path used by this bridge does not support changing the PTC mode remotely. Use the physical thermostat to activate Emergency Heat; HA will reflect the state.

---

## Contributing

Pull requests are welcome!

Areas where help is especially needed:

- **Emergency Heat activation from HA** — the NATC app uses a direct `/v1/luacontrol/json2data` API path with full status context that we cannot replicate with our sessionId-based auth. If you can figure out how to call this endpoint or find another way to toggle PTC mode remotely, that would unlock this feature.
- **Dry mode duration control** — setting 15 / 45 / 60 min via the cloud API (see `send_dry_mode()`)
- **Supplemental PTC detection in normal Heat mode** — requires testing at outdoor temps below -10°C
- **Additional entities** — swing, eco mode, turbo, sleep mode (APK analysis confirms these exist)

To report a bug or suggest an improvement: [open an issue](../../issues).

---

## License

MIT — see [LICENSE](LICENSE)
