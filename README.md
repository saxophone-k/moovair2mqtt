# moovair2mqtt

MQTT bridge for the **Moovair ST-1** central heat pump thermostat, enabling full control and monitoring from **Home Assistant**.

Follows the same conventions as [mysa2mqtt](https://github.com/bourquep/mysa2mqtt).

> **Disclaimer:** This project was built through reverse engineering of the Moovair Android APK. It is not affiliated with or endorsed by Moovair or Midea.
>
> ⚠️ **Stability warning:** This bridge relies entirely on Moovair's **undocumented private cloud API**. If Moovair changes their API, authentication system, or encryption at any time, this bridge may stop working with no warning. Use it knowing this risk.
>
> **A note from the author:** I am not a programmer — this entire project was built with the help of [Claude Code](https://claude.ai/code) (AI-assisted development). If you run into issues or have questions, I'll do my best to help, but please keep in mind that my ability to debug code is very limited. That said, feel free to open an issue — maybe someone in the community can step in! 😄

---

## Features

- ✅ Read ambient temperature, setpoint, HVAC mode, fan mode
- ✅ Full control from Home Assistant (setpoint, mode, fan)
- ✅ Power ON/OFF
- ✅ Modes: Auto, Heat, Cool, Dry, Fan Only, Emergency Heat
- ✅ Fan speeds: Auto, Low, Medium, High
- ✅ Aux-Heat sensor (electric backup element active)
- ✅ Automatic MQTT Discovery (Home Assistant detects the device with zero config)
- ✅ Automatic re-login when session expires
- ⚠️ Automatic aux-heat detection in normal heat mode: requires winter testing (TODO)

---

## Requirements

- **Docker** (to run the bridge as a container)
- **An MQTT broker** — e.g. [Mosquitto](https://mosquitto.org/). If you're on TrueNAS Scale or similar, you likely already have one running.
- **Home Assistant** with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) enabled
- **A Moovair account** (email + password from the Moovair app)

> ⚠️ **Known limitation:** The Moovair cloud only allows **one active session at a time**. The Moovair app and this bridge cannot run simultaneously — the bridge takes over the session.

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
      M2M_POLL_INTERVAL: "5"              # seconds between each poll
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
| `M2M_POLL_INTERVAL` | — | `30` | Poll interval in seconds (recommended minimum: 5) |
| `M2M_LOG_LEVEL` | — | `info` | Log level: debug / info / warning / error |

---

## Home Assistant Entities

| Entity | Type | Description |
|--------|------|-------------|
| Moovair | `climate` | Full thermostat control |
| Moovair Aux-Heat | `binary_sensor` | Electric backup element active |

---

## MQTT Topics

Topics follow the format `{prefix}/{device_id}/{field}`.

### State (bridge → HA)
| Topic | Values |
|-------|--------|
| `.../current_temperature` | e.g. `23.9` |
| `.../target_temperature` | e.g. `23.0` |
| `.../mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `dry` |
| `.../fan_mode` | `auto`, `low`, `medium`, `high` |
| `.../action` | `heating`, `cooling`, `idle`, `off`, `fan`, `drying` |
| `.../aux_heat` | `ON`, `OFF` |
| `.../availability` | `online`, `offline` |

### Commands (HA → bridge)
| Topic | Values |
|-------|--------|
| `.../set/mode` | `off`, `heat`, `cool`, `heat_cool`, `fan_only`, `dry` |
| `.../set/target_temperature` | e.g. `22.0` |
| `.../set/fan_mode` | `auto`, `low`, `medium`, `high` |

---

## How It Works

This bridge communicates with the Moovair cloud (Midea NetHomePlus infrastructure) using a reverse-engineered protocol:

1. **Authentication:** SHA256 signature + AES/ECB session key derived from the login response
2. **State reading:** `app2base/data/transmit` → Lua json2data → `appliance/transparent/send` → AES-encrypted m0 packets
3. **Control:** Same path with a control JSON payload
4. **Temperature storage:** The device stores temperatures internally in Fahrenheit (auto-converted to Celsius)

### Aux-Heat Behavior

The electric backup element (aux heat) activates automatically based on **outdoor temperature**, not setpoint delta. It only kicks in when outdoor temperature drops below the system's configured balance point (typically -5°C to -15°C for cold-climate heat pumps).

- Automatic aux-heat byte detection requires winter testing at outdoor temps below -5°C
- Emergency Heat mode (user-forced aux heat) is fully supported

---

## Contributing

Pull requests are welcome! If you have the same thermostat and can test in cold weather, your help mapping the aux-heat bytes would be much appreciated.

To report a bug or suggest an improvement: [open an issue](../../issues).

---

## License

MIT — see [LICENSE](LICENSE)
