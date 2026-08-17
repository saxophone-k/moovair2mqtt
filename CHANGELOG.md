# Changelog

All notable changes to this project are documented here.

> **⚠ Image tags drop the `v`.** Git tag `v3.1.0` publishes the image
> `ghcr.io/saxophone-k/moovair2mqtt:3.1.0`. There is no `:v3.1.0`.

---

## [3.1.0] — 2026-08-17

**Auxiliary heat, done properly.** Home Assistant now reports the three genuinely
different states of the resistive element instead of one sensor that conflated
them, and it follows the thermostat panel's own Auxiliary Heat switch.

Everything here was measured on real hardware against the panel display.

### Added

- **Aux Heat Allowed** (binary sensor) — mirrors the panel's
  **Settings → Auxiliary Heat** switch. This is the switch that arms or disarms
  the electric element for the season.
- **Aux Heat Drawing** (binary sensor) — **the element is actually energised**,
  burning full power. Read from the appliance status frame the thermostat sends
  to its own cloud client (`aa 62 44`, byte 40 bit 2). **This is a restored
  regression:** the v2 cloud bridge read exactly this bit (see 2.1.0 below); when
  the bridge moved local in v3 it was rebuilt from a different log field and the
  correct signal was lost.
- **`M2M_MQTT_CLIENT_ID`** — override the MQTT client identifier. Rarely needed;
  see the fix below.

### Changed

- **`Aux Heat` is renamed `Aux Heat Armed`.** Its underlying entity ID is
  unchanged, so **your history, dashboards and automations keep working.** Home
  Assistant will keep showing the old name until you rename it in the UI
  (Settings → Devices → the entity → rename); that is cosmetic.
- **The Emergency Heat preset now appears only when the panel allows it.** Turn
  the panel's Auxiliary Heat switch off and the preset — and the preset dropdown
  itself — disappears from the climate card; turn it on and it comes back,
  within seconds. The panel remains the only place that switch can be set (see
  *Known limitations*).
- **Discovery is republished whenever the unit's reported capabilities change**,
  instead of once at startup. This is what lets the preset appear and disappear
  live.

### Fixed

Five bugs, all present in v3.0.0 and v3.0.1. The first two were found by
replaying real captured device logs through the parser.

- **The unit's reported capabilities were never read.** Two patterns competed for
  the same log line and the wrong one won, so `available_mode`, `aux_heat_open`,
  `heating_max`, `cooling_min` and `comm_mode` were never parsed. **Effect:** the
  bridge's capability detection never actually took effect — every install fell
  back to the built-in defaults. Those defaults match the reference unit, so most
  installs looked correct; **if your unit differs from the reference hardware,
  your entities may legitimately change after this upgrade.** That is the
  detection finally working. If something looks wrong, please open an issue with
  your startup log.
- **The on-change capability line was never matched**, because the pattern
  required a field that only appears on the periodic line. This is the line that
  fires the instant you flip the panel switch, so live updates were impossible.
- **An unrecognised mode was hidden instead of kept.** A mode the bridge has no
  bit mapping for was treated as unsupported and dropped from the climate entity.
  The intent has always been the opposite — never hide a mode that works.
- **`available_mode` bit 3 was misread as Heat**; it is Emergency Heat. With
  capability parsing fixed, that error would have **stripped Heat out of the
  climate entity whenever the owner turned auxiliary heat off** — precisely when
  the heat pump is still wanted. Which bit truly means Heat remains unknown, so
  Heat is deliberately never gated on a guess.
- **The MQTT client ID was hardcoded**, so a second instance run for testing
  fought the production instance for the same broker identity and the two kicked
  each other off in a loop — even on a completely separate topic prefix. It is
  now derived from the topic prefix. **Existing installs keep the original ID and
  are unaffected.**

### Known limitations

- **The panel's Auxiliary Heat switch cannot be set remotely.** The thermostat's
  UI layer exposes read and notify for it but no setter, so Home Assistant
  *mirrors* the switch rather than driving it. Set it once at the panel for the
  season.
- **Aux Heat Drawing is verified on the 99-byte status frame only.** A unit
  reporting a different frame length is ignored and logged; please report it.

### Upgrading

Nothing to do. Pull the new image and restart — no configuration changes, no
entity cleanup, no migration steps. You gain two entities and one rename.

---

## [3.0.1] — 2026-08-15

### Fixed

- **Published images were `amd64`-only** and could not run on ARM hardware —
  Home Assistant Green, Home Assistant Yellow, Raspberry Pi or ARM NAS boxes.
  `3.0.1` and `latest` are now built for **`amd64` and `arm64`**.
  *(`3.0.0` and `2.1.0` are deliberately left as-is: a pinned version must never
  change under the people who pinned it.)*

### Added

- **Home Assistant OS add-on instructions** in the README, for installs that
  cannot run arbitrary containers. Marked untested — feedback welcome.

---

## [3.0.0] — 2026-08-15

**The bridge moved off the cloud and onto your LAN.** This is a breaking change;
see [MIGRATION.md](MIGRATION.md).

### Changed

- **Control is now entirely local.** No Midea account, no cloud, no internet.
  The bridge reads the thermostat's own logs and injects commands into the
  internal message queue the vendor's cloud client uses, over ADB on your LAN.
- **Roughly 50× faster.** Commands are injected in ~100–160 ms and confirmed by
  the device in ~200 ms; panel changes appear in Home Assistant essentially
  instantly.
- **It no longer logs you out of the Moovair app**, because it never registers
  for push notifications — it never touches your account at all.
- **`M2M_DEVICE_ID` now derives from the thermostat's IP address** instead of a
  hardcoded appliance ID. **Anyone migrating from v2 must set it explicitly to
  their Midea appliance ID** or Home Assistant will create a second thermostat
  and orphan all existing history.
- Temperature steps are now whole degrees, matching what the panel and the phone
  app actually allow.

### Added

- **Emergency Heat** preset, **Freeze Protection** switch, **Dry Duration** as a
  freely settable number (the app offers four fixed values; the device accepts
  anything), **Indoor Coil Temperature**, and a **°C/°F** setting that follows
  the panel.
- **Self-configuring discovery** — entities are built from what the unit reports
  rather than assumed. *(See the v3.1.0 notes: this did not actually take effect
  until 3.1.0.)*
- **Device availability tracking** — Home Assistant is told the thermostat is
  offline instead of showing stale values.
- `tools/clear_legacy_discovery.py` to clear v2's retained discovery topics.
- [FIRMWARE_BACKUP.md](FIRMWARE_BACKUP.md) — image your firmware while the debug
  port is open, as insurance against a future update closing it.

### Fixed

- A race that could send a command frame mixing two different moments of device
  state (a new setpoint with a stale mode), snapping the thermostat back.
- Changing the setpoint or fan speed while the unit was **off** silently switched
  it on.
- Indoor temperature was read from the raw sensor rather than the compensated
  value the panel itself displays, reading roughly 2 °C high.

---

## [2.1.0] — 2026-05-20

Final cloud-based release, kept as a supported escape hatch. Pin
`ghcr.io/saxophone-k/moovair2mqtt:2.1.0` to stay on it.

### Added

- **Auxiliary heat sensor**, read from the appliance status frame's byte 40
  bit 2 — the true "element energised" signal. *(v3.0.0 rebuilt this from a
  different source and lost it; v3.1.0 restores it as **Aux Heat Drawing**.)*
- Heat-pump status sensor; outdoor coil temperature.

---

## [2.0.0] — 2026-05-19

Cloud-based bridge via the Midea API.

[3.1.0]: https://github.com/saxophone-k/moovair2mqtt/releases/tag/v3.1.0
[3.0.1]: https://github.com/saxophone-k/moovair2mqtt/releases/tag/v3.0.1
[3.0.0]: https://github.com/saxophone-k/moovair2mqtt/releases/tag/v3.0.0
[2.1.0]: https://github.com/saxophone-k/moovair2mqtt/releases/tag/v2.1.0
[2.0.0]: https://github.com/saxophone-k/moovair2mqtt/releases/tag/v2.0.0
