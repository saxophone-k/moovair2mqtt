# Migrating from v2 (cloud) to v3 (local)

v2 talked to your thermostat through Midea's cloud using your Moovair account. **v3 talks to the thermostat directly over your LAN** — no account, no cloud, no internet needed.

It is faster (~200 ms instead of ~11 s), it does not log you out of the Moovair app, and it can do things the cloud API could not. But it is a **breaking change**: the configuration is different, and there is **one setting you must carry over or you will lose all your history**.

**Budget ten minutes.** Nothing here is irreversible — [rolling back](#rolling-back-to-v2) is one line.

---

## The one thing that matters: `M2M_DEVICE_ID`

Home Assistant identifies entities by a `unique_id`. Both v2 and v3 build theirs from your **Midea appliance ID**, so as long as v3 uses the same one, Home Assistant treats the new entities as *the same entities* — your history, dashboards, automations and scripts all keep working, untouched.

**Get it wrong and nothing breaks loudly.** You simply get a second thermostat device alongside the first, with no history, and every dashboard still pointing at the old dead one.

v2 discovered this ID automatically when it logged in, so it is not in your config file. Here is how to find it.

### Find your appliance ID

The ID is embedded in the MQTT discovery topics v2 published. With the v2 bridge **still running or recently stopped** (the topics are retained, so they survive it stopping):

```sh
mosquitto_sub -h YOUR_BROKER_IP -W 3 -v -t 'homeassistant/#' \
  | grep -o 'moovair_[0-9]\{6,\}' | sort -u
```

Expected output — a single line:

```
moovair_151732606682728
```

Your appliance ID is the number after `moovair_`.

**No `mosquitto_sub`?** Either works just as well:
- **MQTT Explorer** — browse to `homeassistant/climate/` and read the topic name
- **Home Assistant** → Settings → Devices & Services → MQTT → *Configure* → **Listen to a topic**, subscribe to `homeassistant/climate/+/config`, and read the topic

---

## Migration steps

### 1. Note your appliance ID

From above. Write it down before you change anything.

### 2. Stop the v2 bridge

Don't delete it yet — leave it stopped until you're happy with v3.

### 3. Find your thermostat's IP and pin it

The thermostat must keep the same address, because the bridge connects to it directly. **Give the thermostat a fixed IP address** — in your router's settings this is usually called a *DHCP reservation* or a *static lease*.

Check that ADB is reachable:

```sh
nc -vz THERMOSTAT_IP 5555
```

If that fails, v3 cannot work — see [Troubleshooting](#troubleshooting).

### 4. Replace your configuration

The account variables are gone. The thermostat address and the appliance ID replace them:

```yaml
services:
  moovair2mqtt:
    image: ghcr.io/saxophone-k/moovair2mqtt:latest
    container_name: moovair2mqtt
    restart: unless-stopped
    environment:
      # ── removed in v3 ────────────────────────────────
      # M2M_MOOVAIR_USERNAME: "..."
      # M2M_MOOVAIR_PASSWORD: "..."

      # ── new in v3 ────────────────────────────────────
      M2M_THERMOSTAT_HOST: "192.168.1.50:5555"       # step 3
      M2M_DEVICE_ID: "151732606682728"               # step 1 — YOUR id

      # ── unchanged ────────────────────────────────────
      M2M_MQTT_HOST: "192.168.1.10"
      M2M_MQTT_PORT: "1883"
```

If you leave the old account variables in place, v3 ignores them and logs a warning. It will not try to use them.

### 5. Clear v2's retained discovery

**Do not skip this.** v2 published its discovery configs *retained*, so they sit on the broker after v2 stops. Home Assistant sees the old config first and keeps the entity bound to v2's dead topics — the classic symptom is **everything reading "Unknown"** even though v3 is clearly running.

```sh
python3 tools/clear_legacy_discovery.py --host YOUR_BROKER_IP \
    --device-id YOUR_APPLIANCE_ID --old-prefix moovair2mqtt
```

It is a **dry run by default** — it prints what it would remove and changes nothing. Review that list, then re-run with `--apply`.

### 6. Start v3

```sh
docker compose up -d
docker compose logs -f
```

You want to see:

```
MQTT connected to ...
ADB (command channel) connected to ...
ADB (read stream) connected
thermostat ONLINE (log stream active)
HA discovery published (modes=[...], range=16-30)
```

### 7. Check Home Assistant

**One** Moovair device, with your original entities and their history intact. Change the setpoint from HA and watch the thermostat panel follow within a second.

---

## What changes, and what doesn't

| | v2 (cloud) | v3 (local) |
|---|---|---|
| Moovair account | required | **not used at all** |
| Internet | required | not required |
| Latency | ~11 s | ~200 ms |
| Logs you out of the Moovair app | yes | **no** |
| Emergency heat | detect only | **activate** |
| Dry duration | fixed 30 min | any value |
| Indoor coil temperatures | ✗ | ✓ |
| Freeze protection | ✗ | ✓ |
| Your HA history | — | **kept**, if `M2M_DEVICE_ID` matches |

Entity names and IDs are unchanged. Two entities are new (Freeze Protection, Indoor Coil Temperature) and will simply appear.

---

## Troubleshooting

**Everything reads "Unknown".** Step 5 was skipped or did not match. Re-run it with `--apply`, then restart the bridge.

**Two Moovair devices in Home Assistant.** `M2M_DEVICE_ID` does not match v2's. Stop the bridge, fix the ID, clear the wrong device's retained topics with the same tool (pass the *wrong* id as `--device-id`), and restart.

**`nc` says 5555 is closed / `ADB connection refused`.**
- Confirm the thermostat's IP.
- If the thermostat is on an IoT VLAN, the machine running the bridge must be able to route to it. This is the most common cause.
- If ADB is genuinely disabled on your unit, v3 cannot work — please open an issue with your firmware version, because that is important for everyone.

**The panel display doesn't follow Home Assistant.** Open an issue. Some command paths repaint the panel and some don't; the bridge deliberately routes everything through the one that does, so this would be a real bug.

---

## Rolling back to v2

v2 is still published and still works. Pin the image:

```yaml
image: ghcr.io/saxophone-k/moovair2mqtt:2.1.0
```

Put your account variables back, and re-add `M2M_THERMOSTAT_HOST`-free config as it was. You may need to clear v3's retained discovery first — same tool, with `--old-prefix` set to whatever v3 was publishing to.

`:2.1.0` is immutable and will not move. Do **not** pin `:latest` expecting v2 — `:latest` is v3 from now on.
