# moovair2mqtt

Control a **Moovair ST-1** central heat-pump thermostat from **Home Assistant** — locally, over your own LAN, with no cloud and no internet dependency.

Moovair is a rebadged **Midea**. If your thermostat's app is Moovair, Midea, or one of the other Midea white-labels, this may work for you too.

```
Home Assistant  ←→  MQTT  ←→  moovair2mqtt  ←→  thermostat (ADB, your LAN)
```

> ## 🔀 v3.0.0 — 15 August 2026 — the bridge moved from the cloud to your LAN
>
> **This is a breaking change.** Everything up to v2.1.0 controlled the thermostat through Midea's cloud, using your Moovair account. **v3 talks to the thermostat directly over your own network** — no account, no cloud, no internet required. It is roughly 50× faster, it no longer logs you out of the Moovair app, and it can do things the cloud API simply could not.
>
> **Were you already running v2 and all of a sudden your integration stopped working? This is normal.** v3.0.0 is a complete redesign from v2.1.0. Read **[MIGRATION.md](MIGRATION.md)** to get everything back up and running. It takes about ten minutes, and there is **one setting** you must carry over or Home Assistant will create a second thermostat and orphan all your history.
>
> **Not ready?** Nothing is forced on you — pin `ghcr.io/saxophone-k/moovair2mqtt:2.1.0` and the cloud bridge keeps working exactly as before.

---

> **Disclaimer:** This project was built through reverse engineering of the Moovair thermostat and its Android app. It is not affiliated with or endorsed by Moovair or Midea.
>
> ⚠️ **Stability warning:** v3 works because the thermostat ships with a debug port (`adbd`) open on your network. It is undocumented and unsupported, and **a firmware update could close it and break this bridge with no warning.** See [Read this before you install](#-read-this-before-you-install) — blocking the thermostat's internet access is the recommended mitigation.
>
> **A note from the author:** I am not a programmer — this entire project was built with AI-assisted development ("vibe-coding", if you like). If you run into issues or have questions, I'll do my best to help, but please keep in mind that my ability to debug code is very limited. That said, feel free to open an issue — maybe someone in the community can step in! 😄

---

## ⚠ Read this before you install

This project works because of a decision the vendor made, not one they documented:

**The thermostat ships with `adbd` enabled, unauthenticated, as root, on TCP 5555.** Nothing is installed on the device and nothing is modified permanently. The bridge connects, reads the device's own logs, and injects commands into the internal message queue that the vendor's own cloud client uses.

What follows from that, and should be accepted before relying on this:

1. **A firmware update could close that door and break everything.** There is no workaround if it happens.
2. **Firmware updates are cloud-triggered.** The device's updater only installs what the cloud stages for it. A real 49.9 MB firmware push landed on 2026-04-29.
3. **Blocking the thermostat's internet access is therefore recommended**, to prevent over-the-air (OTA) updates and future enshittification — see [the trade-off](#the-local-only-trade-off) below.
4. **Blocking it also kills the Moovair phone app**, which is cloud-only. Set up a VPN tunnel (Tailscale, WireGuard) and you can still control the thermostat through Home Assistant from outside your home — which is what the app was for anyway.
5. **Anyone on that network segment has root on the thermostat.** That is true with or without this bridge, but you should know it. Put the thermostat on an IoT VLAN.

**If you currently run the cloud-based bridge for this thermostat, turn it off first.** Cloud bridges register for push notifications, which replaces your phone's token and **logs you out of the Moovair app**. This bridge never touches your Midea account, so it does not do that.

---

## What you get

10 entities, published via MQTT discovery — they appear in Home Assistant on their own.

| Entity | Type | Notes |
|---|---|---|
| **Moovair** | Climate | Off / Auto / Cool / Heat / Fan only, fan Auto·Low·Med·High, whole degrees |
| **Emergency Heat** | Climate preset | Heat pump bypassed, resistive element only |
| **Freeze Protection** | Switch | The panel's 8 °C freeze-protect mode (heat mode only) |
| **Dry Mode** | Switch | Start / stop dehumidify |
| **Dry Duration** | Number | 5–120 min, freely settable |
| **Dry Mode Remaining** | Sensor | Counts down once per minute |
| **Indoor Humidity** | Sensor | % |
| **Indoor Coil Temperature** | Sensor | °C |
| **Outdoor Coil Temperature** | Sensor | °C — condenser when cooling, evaporator when heating |
| **Aux Heat** | Binary sensor | The resistive element actually drawing power |
| **Heat Pump** | Binary sensor | Compressor running |

**It is fast.** State changes made at the panel appear in Home Assistant essentially instantly. Commands are injected in ~100–160 ms and confirmed by the device in ~200 ms.

**Things the cloud API could not do, and this can:**

- **Set any dry-mode duration.** The app offers four fixed buttons; the device accepts anything.
- **Read indoor coil temperatures**, which the cloud never exposed.
- **Run alongside the Moovair app**, because it never logs into your account.

---

## Requirements

- The thermostat, reachable on your network. **Give it a fixed IP address, or the bridge will stop working when the address changes** — in your router this is usually called a *DHCP reservation* or *static lease*.
- An **MQTT broker** (e.g. Mosquitto) and Home Assistant's MQTT integration.
- Docker, or any way to run a Python 3.11+ script.

You do **not** need `adb` installed. The bridge speaks the ADB protocol itself in pure Python.

---

## Is my unit supported?

Only one hardware variant has been tested on real hardware. The bridge **detects what your unit reports and builds entities to match** — it omits an entity rather than publishing a guess, and logs anything it does not recognise so it can be reported.

| | Status |
|---|---|
| **2-wire communicating bus** | ✅ **Verified on real hardware** |
| **24 V conventional multi-wire** | ⚠️ **Decoded but UNTESTED — testers wanted** |
| **With PTC resistive element** | ✅ Verified |
| **Heat-pump only (no PTC)** | ⚠️ Untested — the PTC is an add-on, so some installs will not have one |
| **°C** | ✅ Verified |
| **°F** | ✅ Verified |
| **Docker / TrueNAS SCALE** | ✅ Verified — this is how I run it |
| **Home Assistant OS add-on** | ⚠️ **Untested — instructions provided, feedback wanted** |

If you have one of the untested variants, please open an issue with the bridge's startup log — that is exactly the information needed.

---

## Install

### Docker Compose

```yaml
services:
  moovair2mqtt:
    image: ghcr.io/saxophone-k/moovair2mqtt:latest
    container_name: moovair2mqtt
    restart: unless-stopped
    environment:
      M2M_THERMOSTAT_HOST: "192.168.1.50:5555"   # your thermostat
      M2M_MQTT_HOST: "192.168.1.10"              # your broker
      M2M_MQTT_PORT: "1883"
      # M2M_MQTT_USERNAME: ""
      # M2M_MQTT_PASSWORD: ""
```

```sh
docker compose up -d
docker compose logs -f
```

You are looking for `ADB (command channel) connected`, `MQTT connected`, and `HA discovery published`.

### Home Assistant OS (HA Green, HA Yellow, Raspberry Pi) — ⚠️ UNTESTED

> **I have not tested this.** I run the bridge as a container on TrueNAS, not as a
> Home Assistant add-on, and I have no HA OS machine to verify against. The steps
> below are what *should* work. **If you try it, please open an issue and say what
> you had to change** — I will fold it in and drop this warning.

Home Assistant OS cannot run arbitrary containers; software has to be packaged as
an **add-on**. There is no official add-on for this bridge yet, so you build a
local one. It is about ten lines of configuration.

**1. Get access to the `/addons` folder** — install either the *Samba share* or
*Advanced SSH & Web Terminal* add-on from the official add-on store.

**2. Create `/addons/moovair2mqtt/` and put three files in it.**

`config.yaml`:

```yaml
name: moovair2mqtt
version: "3.0.0"
slug: moovair2mqtt
description: Local control of a Moovair ST-1 thermostat, no cloud
arch: [aarch64, amd64, armv7]
init: false
options:
  thermostat_host: "192.168.1.50:5555"
  mqtt_host: "192.168.1.10"
  mqtt_port: 1883
  mqtt_username: ""
  mqtt_password: ""
  device_id: ""
schema:
  thermostat_host: str
  mqtt_host: str
  mqtt_port: port
  mqtt_username: str?
  mqtt_password: password?
  device_id: str?
```

`Dockerfile`:

```dockerfile
ARG BUILD_FROM=ghcr.io/saxophone-k/moovair2mqtt:3.0.0
FROM ${BUILD_FROM}
USER root
RUN pip install --no-cache-dir bashio 2>/dev/null || true
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
```

`run.sh` — **this is the part people miss.** Add-ons receive their settings as
`/data/options.json`, *not* as environment variables, so they have to be
translated:

```sh
#!/bin/sh
CONF=/data/options.json
export M2M_THERMOSTAT_HOST=$(sed -n 's/.*"thermostat_host": *"\([^"]*\)".*/\1/p' $CONF)
export M2M_MQTT_HOST=$(sed -n 's/.*"mqtt_host": *"\([^"]*\)".*/\1/p' $CONF)
export M2M_MQTT_PORT=$(sed -n 's/.*"mqtt_port": *\([0-9]*\).*/\1/p' $CONF)
export M2M_MQTT_USERNAME=$(sed -n 's/.*"mqtt_username": *"\([^"]*\)".*/\1/p' $CONF)
export M2M_MQTT_PASSWORD=$(sed -n 's/.*"mqtt_password": *"\([^"]*\)".*/\1/p' $CONF)
export M2M_DEVICE_ID=$(sed -n 's/.*"device_id": *"\([^"]*\)".*/\1/p' $CONF)
exec python -u /app/moovair2mqtt.py
```

**3. Install it.** Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**,
then look under **Local add-ons**. Open it, fill in the configuration tab, start.

**4. Check the log** for `ADB (command channel) connected` and
`HA discovery published`.

**Before you start**, confirm the thermostat is reachable from your Home Assistant
machine — from the SSH add-on terminal:

```sh
nc -vz YOUR_THERMOSTAT_IP 5555
```

If that does not connect, the bridge cannot work; check that the thermostat's
VLAN is reachable from Home Assistant.

### TrueNAS SCALE

**Apps → Discover Apps → Custom App.** Image repository `ghcr.io/saxophone-k/moovair2mqtt`, tag `latest`, restart policy *Unless Stopped*. Add the environment variables above. **No ports and no storage are needed** — the bridge only makes outbound connections and stores nothing.

---

## Configuration

| Variable | Required | Default | What it does |
|---|---|---|---|
| `M2M_THERMOSTAT_HOST` | ✅ | — | `ip:5555` of the thermostat |
| `M2M_MQTT_HOST` | ✅ | — | MQTT broker address |
| `M2M_MQTT_PORT` | | `1883` | |
| `M2M_MQTT_USERNAME` | | — | If your broker requires auth |
| `M2M_MQTT_PASSWORD` | | — | |
| `M2M_MQTT_TOPIC_PREFIX` | | `moovair2mqtt` | State/command topic root |
| `M2M_HA_DISCOVERY_PREFIX` | | `homeassistant` | Match your HA setting |
| `M2M_DEVICE_ID` | | derived from the IP | **Seeds every entity's `unique_id`.** See below |
| `M2M_CLOUD_MODE` | | `alongside` | `alongside` or `local_only` |
| `M2M_QUERY_INTERVAL` | | `60` | Safety-net re-query, seconds. `0` disables |
| `M2M_HEARTBEAT_TIMEOUT` | | `30` | Seconds of silence before marking the device offline |
| `M2M_LOG_LEVEL` | | `info` | `debug` is very noisy |

### `M2M_DEVICE_ID` — the one to think about

It seeds every entity's `unique_id`, so **it must stay stable for the life of the install**. Change it and Home Assistant creates a fresh set of entities and orphans your history, dashboards and automations.

By default it is derived from the thermostat's IP address, which is fine since we recommended giving it a fixed IP. Set it explicitly if you ever change the thermostat's IP — or if you are **migrating from v2**, in which case it must be your Midea appliance ID. See [MIGRATION.md](MIGRATION.md).

---

## The local-only trade-off

The bridge works whether or not the thermostat can reach the internet. Blocking it is a real decision with a real cost:

| | WAN open (`alongside`) | WAN blocked (`local_only`) |
|---|---|---|
| Home Assistant | ✅ | ✅ |
| Moovair phone app | ✅ works alongside | ❌ dead — it is cloud-only |
| Weather icon on the panel | ✅ | ❌ lost — it is AccuWeather, fetched via the cloud |
| Vendor firmware updates | ⚠️ exposed | ✅ protected |
| Privacy | ⚠️ telemetry to the vendor | ✅ nothing leaves your LAN |
| Vendor enshittification | ⚠️ exposed | ✅ protected |

Set `M2M_CLOUD_MODE: "local_only"` when you have firewalled the device, and the bridge stops advertising what it cannot deliver.

**Recommended:** block the thermostat's WAN access at your router, keep LAN access. You lose a decorative weather icon and the phone app; you gain immunity from vendor updates breaking your thermostat.

---

## How it works

Two channels, both over one ADB connection to the device:

- **Read** — tail the thermostat's own `logread`. The vendor's control daemon logs its full parsed state in plaintext, several times a second. No polling, no crypto.
- **Write** — inject command frames into the System V message queue that the vendor's own cloud client writes to. The device's own daemon builds the serial frame for the control board. We never craft control-board bytes by hand.

Because both sides are the vendor's own paths, the panel display, the phone app and Home Assistant all stay in sync.

`msgtool` is a small statically linked ARM helper that performs the queue write. It is **pushed to the thermostat automatically** on startup and after any device reboot (the device's `/tmp` is volatile). Source and build instructions: [`msgtool/`](msgtool/).

---

## Troubleshooting

**Everything reads "Unknown" after upgrading from v2** — stale retained discovery from the old bridge is shadowing the new one. Run [`tools/clear_legacy_discovery.py`](tools/clear_legacy_discovery.py); see [MIGRATION.md](MIGRATION.md).

**`ADB connection refused`** — confirm the thermostat's IP, and that whatever runs the bridge can route to it. If your thermostat is on an IoT VLAN, that VLAN must be reachable from the container's host.

**Two thermostats in Home Assistant** — you changed `M2M_DEVICE_ID` or the topic prefix, so a second set of entities was created. Clear the old ones with the tool above.

**The panel display does not follow Home Assistant** — please open an issue. Some commands notify the panel and some do not; the bridge deliberately routes everything the panel cares about through the channel that repaints it.

---

## Contributing

Most valuable right now: **reports from untested hardware** — 24V wiring configuration and heat-pump-only units without the PTC element. The startup log lists everything the bridge detected, and that is enough to tell whether it guessed right.

## License

MIT — see [LICENSE](LICENSE).

**No affiliation with Midea, Moovair, or any reseller.** This is unofficial, built by reverse engineering, and could stop working at any time.
