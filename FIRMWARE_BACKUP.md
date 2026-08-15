# Back up your thermostat's firmware (do this while ADB still works)

This bridge works because the thermostat ships with a debug port (ADB) open.
**A future firmware update could close that port** and take local control away —
the exact enshittification this project exists to resist.

The single best insurance is to **image your device's firmware now, while you
still have access.** If the vendor ever locks you out, you would have a
known-good copy to flash back to. Ten minutes now buys you a way home later.

> ⚠️ **Making a backup is safe and read-only. *Restoring* one is not documented
> or tested here, and flashing firmware wrong can permanently brick the device.**
> Treat the backup as insurance whose claim process you'd have to work out
> carefully (or with help) if you ever needed it. Everything below is at your
> own risk.

---

## What you need

- The thermostat reachable over ADB (the same access the bridge uses).
- `adb` on your computer, **or** the pure-Python `adb-shell` this project already
  installs.
- ~150 MB of free disk space.

## The device layout

The ST-1 is an Allwinner NAND device with an A/B partition scheme. Confirm yours
matches before trusting the recipe:

```sh
adb shell cat /proc/mtd
adb shell ls -l /dev/by-name/
```

You should see `boot0`/`uboot` (bootloader), `bootA`/`bootB` (kernels),
`rootfsA`/`rootfsB` (the OS, ~126 MB each), and some small volumes.

## The generic firmware — safe to keep and to share

These carry no personal data and are identical on every ST-1. This is the set
worth having:

| Save | Device node |
|---|---|
| boot0 | `/dev/mtd0ro` |
| uboot | `/dev/mtd1ro` |
| boot-resource | `/dev/ubi0_1` |
| bootA | `/dev/ubi0_4` |
| bootB | `/dev/ubi0_5` |
| rootfsA | `/dev/ubi0_6` |
| rootfsB | `/dev/ubi0_7` |
| dsp0 | `/dev/ubi0_8` |

### ⚠️ Do NOT back up these to anywhere public

`private`, `secure_storage`, `env`/`env-redund`, and the `UDISK` overlay are
**per-device and contain secrets** — device keys, and your **Wi-Fi password** in
the case of `UDISK`. Back them up for yourself if you want a full personal
snapshot, but never share or upload them.

## Recipe

The device's `/tmp` is small, so small volumes are dumped-then-pulled and the big
`rootfs` volumes are gzipped on the way out (the empty space compresses away).

```sh
mkdir fw_backup && cd fw_backup

# small volumes: dump, checksum, pull, verify, clean up
for pair in "boot0:/dev/mtd0ro" "uboot:/dev/mtd1ro" "boot-resource:/dev/ubi0_1" \
            "bootA:/dev/ubi0_4" "bootB:/dev/ubi0_5" "dsp0:/dev/ubi0_8"; do
  name=${pair%%:*}; node=${pair##*:}
  adb shell "dd if=$node of=/tmp/fw.img bs=64k 2>/dev/null; sha256sum /tmp/fw.img"
  adb pull /tmp/fw.img "$name.img"
  adb shell rm -f /tmp/fw.img
  sha256sum "$name.img"      # compare this to the on-device hash above
done

# rootfs (too big for /tmp): gzip on device, then verify the decompressed hash
for pair in "rootfsA:/dev/ubi0_6" "rootfsB:/dev/ubi0_7"; do
  name=${pair%%:*}; node=${pair##*:}
  adb shell "dd if=$node bs=64k 2>/dev/null | sha256sum"   # note this hash
  adb shell "dd if=$node bs=64k 2>/dev/null | gzip -6 > /tmp/fw.gz"
  adb pull /tmp/fw.gz "$name.img.gz"
  adb shell rm -f /tmp/fw.gz
  gzip -dc "$name.img.gz" | sha256sum                       # must match the hash above
done
```

**Always verify the checksums match** device-side and after pulling. A corrupt
backup is worse than none — it fails silently the day you need it.

Store the result somewhere durable (and offline is fine). That's it: you now hold
a verified image of your thermostat's firmware from before any vendor lockout.

---

## If your ADB is already closed

If you found this project *after* an update already closed port 5555, you can't
make a backup — but you may not be stuck. A **known-good image of this exact
hardware exists** (2-wire ST-1). Open an issue describing your situation and your
firmware version; recovery isn't guaranteed and isn't a solved procedure, but
you would not be starting from nothing. 😉
