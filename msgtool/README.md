# msgtool

A ~3 KB helper that writes one message to a System V message queue on the thermostat. It is how the bridge sends commands.

**You do not need to build this.** The compiled binary is committed here, and the bridge pushes it to the thermostat automatically on startup and after any device reboot (the device's `/tmp` is wiped on reset). Build it yourself only if you want to verify what you are running, or if you are porting to another device.

## Why it exists

The thermostat's own control daemon (`dev_app`) receives commands over SysV message queue **msqid 1** — the same queue the vendor's cloud client writes to. Sending a command means placing a correctly framed message on that queue.

Doing that requires the `msgsnd()` syscall, from a process running on the device. There is no shell built-in for it and the device has no scripting language that exposes it, so a small native binary is the shortest path. It is deliberately minimal: **one operation per invocation, no looping, no draining.**

The payoff is that we never craft control-board bytes by hand — we hand the vendor's own daemon a command in its own format and let it build the serial frame. That is why the panel display and the phone app stay in sync with Home Assistant.

## Building

The thermostat is 32-bit ARM, and the binary is linked **statically** so it depends on nothing present on the device.

```sh
sudo apt install gcc-arm-linux-gnueabihf          # Debian/Ubuntu
arm-linux-gnueabihf-gcc -static -O2 -o msgtool msgtool.c
```

Verify what you produced:

```sh
$ file msgtool
msgtool: ELF 32-bit LSB executable, ARM, EABI5 version 1 (GNU/Linux),
         statically linked, ... for GNU/Linux 3.2.0, not stripped
```

**The build is reproducible.** Compiling the committed source with the command above yields BuildID `e6f6f1b62ea596318a4d7621f5783e2ce3f94df3` — the same as the committed binary. Check yours matches:

```sh
readelf -n msgtool | grep -A1 "Build ID"
```

## Usage

```
msgtool send <msqid> <hex>    msgsnd a message built from a hex string.
                              First 8 hex chars = mtype (little-endian long),
                              the rest = mtext.

msgtool peek <msqid>          Non-blocking msgrcv of ONE message, hexdumped.
```

⚠ **`peek` is destructive** — it *removes* the message from the queue. Never point it at a live channel; a command meant for the thermostat will be consumed instead of delivered. It exists for inspecting dead queues during reverse engineering.

## Safety

Writing to msqid 1 is the same thing the vendor's own cloud client does, so the daemon is built to handle it. In extensive testing `dev_app` never crashed from an injected message, and it validates nothing — it forwards to the control board, which corrects anything invalid within ~20 ms.

The control board, not this tool, is the final authority on what the hardware will actually do.
