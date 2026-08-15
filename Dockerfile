# moovair2mqtt v3 — LOCAL bridge (no cloud, no Midea account).
#
# Build:  docker build -t moovair2mqtt:local .
#
# The v2 cloud bridge lived here until v3.0.0. It is preserved at the git tag
# `v2.1.0` and as the published image `ghcr.io/saxophone-k/moovair2mqtt:2.1.0`.

FROM python:3.12-slim

WORKDIR /app

# Dependencies first: Docker caches this layer, so editing the bridge code
# rebuilds in seconds instead of re-installing Python packages every time.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The bridge itself.
COPY moovair2mqtt.py .

# msgtool — a statically linked ARM binary.
#
# It is NEVER executed inside this container. The bridge pushes it to the
# thermostat over ADB and runs it *there*, because the device's /tmp is
# volatile and loses it on every reboot. That is why an ARM binary sits
# happily inside an x86_64 image: it is cargo, not code.
#
# /app/msgtool is the bridge's built-in default for M2M_MSGTOOL_PATH, so no
# environment variable is needed.
COPY msgtool/msgtool /app/msgtool

# Non-root. The bridge only opens *outbound* TCP connections (ADB 5555 to the
# thermostat, MQTT 1883 to the broker), so it needs no privileges whatsoever.
RUN useradd -r -u 1001 moovair
USER moovair

# -u = unbuffered stdout, so logs appear in `docker logs` immediately rather
# than sitting in a pipe buffer until the process exits.
CMD ["python", "-u", "moovair2mqtt.py"]
