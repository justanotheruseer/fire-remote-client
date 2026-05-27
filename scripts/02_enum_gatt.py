#!/usr/bin/env python3
"""Phase 2: enumerate the remote's GATT tree via BlueZ over D-Bus.

bleak can't attach to this remote (LE Privacy + it doesn't advertise while
connected), so we read the resolved GATT tree straight from BlueZ's D-Bus
ObjectManager instead. The remote must be bonded and currently connected (wake
it with a button press first).

Emits captures/gatt_enum_<utc>.yaml with services, characteristics, flags,
descriptors, and annotations:
  * candidate_reason — 128-bit vendor UUID / notify / writable (the voice path
    is a write+notify pattern, though on this remote the audio actually rides
    HID-over-GATT — see README)
  * danger — DFU/OTA-looking surfaces that must not be fuzzed

Usage:
    uv run python scripts/02_enum_gatt.py --mac AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

import yaml
from dbus_fast import BusType
from dbus_fast.aio import MessageBus

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES = REPO_ROOT / "captures"

BORING_SERVICES = {
    "00001800": "Generic Access",
    "00001801": "Generic Attribute",
    "0000180a": "Device Information",
    "0000180f": "Battery Service",
    "00001812": "Human Interface Device",
}
# Firmware-update surfaces — NEVER fuzz these.
DFU_MARKERS = {
    "0000fe59": "Nordic Secure DFU",
    "00001530-1212-efde-1523-785feabcd123": "Nordic legacy DFU",
    "fe151500-5e8d-11e6-8b77-86f30ca893d3": "Amazon vendor service (config/OTA-shaped)",
}


def short(uuid: str) -> str:
    u = uuid.lower()
    return u.split("-")[0] if u.endswith("-0000-1000-8000-00805f9b34fb") else u


def is_vendor(uuid: str) -> bool:
    return not uuid.lower().endswith("-0000-1000-8000-00805f9b34fb")


def annotate(svc_uuid: str, char_uuid: str, flags: list[str]) -> dict:
    out: dict = {}
    for marker, name in DFU_MARKERS.items():
        if marker in (svc_uuid.lower(), char_uuid.lower()) or marker in svc_uuid.lower():
            out["danger"] = f"{name} — do NOT write/fuzz"
    if short(svc_uuid) in BORING_SERVICES:
        return out
    reasons = []
    if is_vendor(char_uuid):
        reasons.append("128-bit vendor UUID")
    if "notify" in flags or "indicate" in flags:
        reasons.append("notify/indicate (possible data stream)")
    if any(f.startswith("write") for f in flags):
        reasons.append("writable (possible control point)")
    if reasons:
        out["candidate_reason"] = "; ".join(reasons)
    return out


async def enumerate_gatt(mac: str) -> dict:
    dev_tag = "dev_" + mac.upper().replace(":", "_")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    root = bus.get_proxy_object("org.bluez", "/", await bus.introspect("org.bluez", "/"))
    objs = await root.get_interface("org.freedesktop.DBus.ObjectManager").call_get_managed_objects()

    # First pass: index services and descriptors by path.
    services: dict[str, dict] = {}
    descriptors: dict[str, list] = {}
    connected = False
    for path, ifaces in objs.items():
        if dev_tag not in path:
            continue
        if "org.bluez.Device1" in ifaces:
            prop = ifaces["org.bluez.Device1"].get("Connected")
            connected = bool(prop.value) if prop else False
        if "org.bluez.GattService1" in ifaces:
            p = ifaces["org.bluez.GattService1"]
            services[path] = {"uuid": p["UUID"].value, "primary": p["Primary"].value,
                              "characteristics": []}
        if "org.bluez.GattDescriptor1" in ifaces:
            p = ifaces["org.bluez.GattDescriptor1"]
            descriptors.setdefault(p["Characteristic"].value, []).append(p["UUID"].value)

    if not services:
        raise SystemExit(
            f"No GATT objects for {mac}. Bond + connect it first (wake with a "
            "button press), then retry. Connected={}".format(connected))

    # Second pass: attach characteristics to their services.
    for path, ifaces in objs.items():
        if dev_tag not in path or "org.bluez.GattCharacteristic1" not in ifaces:
            continue
        p = ifaces["org.bluez.GattCharacteristic1"]
        svc_path = p["Service"].value
        svc_uuid = services.get(svc_path, {}).get("uuid", "?")
        char_uuid = p["UUID"].value
        flags = list(p["Flags"].value)
        entry = {
            "uuid": char_uuid,
            "short_uuid": short(char_uuid),
            "handle": int(path.split("char")[-1], 16),
            "flags": flags,
            "descriptors": descriptors.get(path, []),
            **annotate(svc_uuid, char_uuid, flags),
        }
        if svc_path in services:
            services[svc_path]["characteristics"].append(entry)

    catalog = {
        "captured_utc": dt.datetime.now(dt.UTC).isoformat(),
        "address": mac,
        "connected": connected,
        "services": [
            {
                "uuid": s["uuid"],
                "short_uuid": short(s["uuid"]),
                "is_boring_sig": short(s["uuid"]) in BORING_SERVICES,
                "characteristics": sorted(s["characteristics"], key=lambda c: c["handle"]),
            }
            for s in services.values()
        ],
    }
    return catalog


def summarize(catalog: dict) -> None:
    print("\n=== candidates / danger (boring SIG services excluded) ===")
    for svc in catalog["services"]:
        for ch in svc["characteristics"]:
            if "candidate_reason" in ch:
                print(f"  {ch['uuid']}  [{','.join(ch['flags'])}]")
                print(f"      in {svc['uuid']} — {ch['candidate_reason']}")
            if "danger" in ch:
                print(f"  !! {ch['uuid']}  {ch['danger']}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True, help="bonded+connected remote MAC")
    args = ap.parse_args()

    catalog = await enumerate_gatt(args.mac)
    CAPTURES.mkdir(exist_ok=True)
    out = CAPTURES / f"gatt_enum_{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.yaml"
    out.write_text(yaml.safe_dump(catalog, sort_keys=False, width=100))
    print(f">> wrote {out}")
    summarize(catalog)


if __name__ == "__main__":
    asyncio.run(main())
