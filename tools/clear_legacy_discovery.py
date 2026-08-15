#!/usr/bin/env python3
"""
Clear the v2 (cloud bridge) retained MQTT topics so v3 (local) can take over.

WHY THIS IS NEEDED
------------------
Home Assistant keys entities by `unique_id`. The v2 cloud bridge published its
discovery configs *retained*, so they survive the app being stopped. v3 uses the
same unique_ids (deliberately — so your history and dashboards carry over), which
means HA sees the old config first and treats the new one as a duplicate. The
entity stays bound to v2's now-dead topics and shows "Unknown", often still
displaying v2's last frozen values.

A retained message is cleared by publishing an EMPTY payload to the same topic
with retain=True.

SAFETY
------
- Dry run by default. Nothing is deleted without --apply.
- Only touches topics that belong to the OLD bridge, identified by the fact that
  their discovery payload references the old state-topic prefix.
- Fully reversible: restarting the v2 app republishes its own discovery.

USAGE
-----
  python3 clear_legacy_discovery.py --host 192.168.1.x                  # list
  python3 clear_legacy_discovery.py --host 192.168.1.x --apply          # clear
  ... --device-id 151732606682728 --old-prefix moovair2mqtt
"""

import argparse
import json
import sys
import time

import paho.mqtt.client as mqtt


def make_client(cid):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    except (AttributeError, TypeError):        # paho 1.x
        return mqtt.Client(client_id=cid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--device-id", default="151732606682728")
    ap.add_argument("--old-prefix", default="moovair2mqtt",
                    help="v2 state topic prefix (default: moovair2mqtt)")
    ap.add_argument("--discovery-prefix", default="homeassistant")
    ap.add_argument("--collect", type=float, default=8.0,
                    help="seconds to gather retained messages")
    ap.add_argument("--apply", action="store_true",
                    help="actually clear (default is a dry run)")
    args = ap.parse_args()

    # Two different topic layouts have to be recognised:
    #   v2 (cloud):  <prefix>/<device-id>/indoor_humidity
    #   v3 (local):  <prefix>/indoor_humidity        <- no device-id segment
    # Matching on the prefix alone covers both, and also handles the case of a
    # v3 install simply being moved to a different prefix (e.g. off the test
    # prefix at cutover), which the <prefix>/<device-id> form missed entirely.
    old_state_root = f"{args.old_prefix}/"
    disc_filter = f"{args.discovery_prefix}/+/moovair_{args.device_id}/#"

    found = {}

    def on_connect(c, u, f, rc, props=None):
        c.subscribe(disc_filter)
        c.subscribe(f"{args.old_prefix}/#")

    def on_message(c, u, msg):
        if msg.payload:                        # ignore already-cleared topics
            found[msg.topic] = msg.payload

    cli = make_client("m2m-clear-legacy")
    if args.username:
        cli.username_pw_set(args.username, args.password or "")
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(args.host, args.port, 30)
    cli.loop_start()
    time.sleep(args.collect)

    stale = []
    for topic, payload in sorted(found.items()):
        if topic.startswith(old_state_root):
            stale.append((topic, "old state"))
            continue
        if topic.endswith("/config"):
            try:
                cfg = json.loads(payload)
            except ValueError:
                continue
            # A stale discovery config is one whose topics still point at the
            # old prefix.
            if old_state_root in json.dumps(cfg):
                stale.append((topic, f"old discovery ({cfg.get('name', '?')})"))

    if not stale:
        print("Nothing stale found — either already cleared, or the "
              "--old-prefix/--device-id do not match your setup.")
        cli.loop_stop()
        return 0

    print(f"\n{len(stale)} legacy retained topic(s) found:\n")
    for topic, why in stale:
        print(f"  [{why}]\n    {topic}")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to clear these.")
        cli.loop_stop()
        return 0

    print("\nClearing...")
    for topic, _ in stale:
        cli.publish(topic, payload=b"", retain=True, qos=1)
    time.sleep(2.0)
    cli.loop_stop()
    print(f"Cleared {len(stale)} topic(s).")
    print("Home Assistant should drop the old entities within a few seconds; "
          "the v3 bridge will re-create them on its next discovery publish "
          "(restart the bridge if it does not happen promptly).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
