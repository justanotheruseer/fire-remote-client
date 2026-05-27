#!/usr/bin/env python3
"""Phase 3: capture the Fire remote's voice audio via hidraw.

The audio rides HID-over-GATT, not the vendor GATT services. BlueZ's input
plugin claims the HID service, so direct GATT writes return "Operation Not
Authorized". The kernel, however, exposes the remote as a hidraw node we can
read/write directly.

Handshake (reactive — a cold enable does nothing):
  press voice button -> remote emits a consumer report carrying the voice key
  -> we write the 1-byte OUTPUT report = 0x01 (mic ENABLE)
  -> 80-byte INPUT reports stream (~50/s, one Opus packet each) while held
  -> on release we write 0x00 (mic DISABLE)

Every report (buttons included) is logged as {monotonic_ns:u64}{len:u16}{bytes},
so the same capture also feeds button-mapping. A JSON sidecar records the
detected report map and per-id histogram.

Needs root for /dev/hidraw* (run via doas/sudo). Pure stdlib so it runs without
the project venv.

Usage:
    doas python3 scripts/03_capture_voice.py --mac AA:BB:CC:DD:EE:FF
    doas python3 scripts/03_capture_voice.py --mac .. --duration 15
    doas python3 scripts/03_capture_voice.py --mac .. --no-enable   # buttons only
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import select
import struct
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES = REPO_ROOT / "captures"
SYS_HIDRAW = "/sys/class/hidraw"


def find_hidraw(mac: str) -> str:
    """Match a /dev/hidrawN node to the remote by its BLE MAC (HID_UNIQ)."""
    mac = mac.lower()
    for path in glob.glob(f"{SYS_HIDRAW}/hidraw*"):
        try:
            uevent = Path(path, "device", "uevent").read_text()
        except OSError:
            continue
        if f"hid_uniq={mac}" in uevent.lower():
            return f"/dev/{os.path.basename(path)}"
    raise SystemExit(
        f"No hidraw node with HID_UNIQ={mac}. Is the remote connected? "
        f"(wake it; check: grep -ril {mac} {SYS_HIDRAW}/*/device/uevent)"
    )


def parse_report_descriptor(buf: bytes) -> dict[int, dict]:
    """Minimal HID report-descriptor walk -> {report_id: {dir, bytes}}.

    Tracks Report ID / Size / Count and accumulates payload bytes per report on
    each Input/Output main item. Enough to auto-pick the audio (largest input)
    and enable (smallest output) reports without hardcoding 0xF0/0xF2.
    """
    reports: dict[int, dict] = {}
    rid = 0
    rsize = rcount = 0
    i = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        if b == 0xFE:  # long item — skip
            if i < len(buf):
                i += 2 + buf[i]
            continue
        tag, typ, size = b >> 4, (b >> 2) & 3, b & 3
        nbytes = (0, 1, 2, 4)[size]  # bSize: 0=>0 bytes, 1=>1, 2=>2, 3=>4
        data = int.from_bytes(buf[i:i + nbytes], "little") if nbytes else 0
        i += nbytes
        if typ == 1:  # Global
            if tag == 0x7:   # Report Size
                rsize = data
            elif tag == 0x9: # Report Count
                rcount = data
            elif tag == 0x8: # Report ID
                rid = data
        elif typ == 0:  # Main
            if tag in (0x8, 0x9):  # Input / Output
                direction = "input" if tag == 0x8 else "output"
                payload = rcount * rsize // 8
                r = reports.setdefault(rid, {"input": 0, "output": 0})
                r[direction] += payload
    return reports


def pick_reports(reports: dict[int, dict]) -> tuple[int, int]:
    """(audio_report_id, enable_report_id) = largest input, smallest output."""
    inputs = {rid: r["input"] for rid, r in reports.items() if r.get("input")}
    outputs = {rid: r["output"] for rid, r in reports.items() if r.get("output")}
    if not inputs or not outputs:
        raise SystemExit(f"Could not find input+output reports in descriptor: {reports}")
    audio = max(inputs, key=inputs.get)
    enable = min(outputs, key=outputs.get)
    return audio, enable


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True, help="remote BLE MAC (to find the hidraw node)")
    ap.add_argument("--hidraw", help="override hidraw device path")
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--out", help="output .bin (default captures/voice_<utc>.bin)")
    ap.add_argument("--voice-key", type=lambda s: int(s, 0), default=0x21,
                    help="consumer-report byte that signals the voice button (default 0x21)")
    ap.add_argument("--audio-report", type=lambda s: int(s, 0), help="override audio report id")
    ap.add_argument("--enable-report", type=lambda s: int(s, 0), help="override enable report id")
    ap.add_argument("--no-enable", action="store_true",
                    help="don't drive the mic handshake (capture buttons only)")
    args = ap.parse_args()

    dev = args.hidraw or find_hidraw(args.mac)
    rdesc_path = Path(SYS_HIDRAW, os.path.basename(dev), "device", "report_descriptor")
    reports = parse_report_descriptor(rdesc_path.read_bytes())
    auto_audio, auto_enable = pick_reports(reports)
    audio_id = args.audio_report if args.audio_report is not None else auto_audio
    enable_id = args.enable_report if args.enable_report is not None else auto_enable
    print(f">> device={dev}  audio_report=0x{audio_id:02x}  enable_report=0x{enable_id:02x}",
          file=sys.stderr)

    CAPTURES.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else \
        CAPTURES / f"voice_{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.bin"
    sidecar = out.with_suffix(out.suffix + ".json")

    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    binf = out.open("wb")
    counts: dict[int, int] = {}
    audio_frames = 0
    enabled = False

    def write_enable(val: int) -> None:
        try:
            os.write(fd, bytes([enable_id, val]))
            print(f">> {enable_id:#04x} <- {val:#04x} "
                  f"({'mic ENABLE' if val else 'mic disable'})", file=sys.stderr)
        except OSError as e:
            print(f"!! enable write failed: {e}", file=sys.stderr)

    deadline = time.monotonic() + args.duration if args.duration else None
    print(">> capturing. Press buttons / hold the voice button. Ctrl-C to stop.",
          file=sys.stderr)
    try:
        while deadline is None or time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(fd, 512)
            except OSError:
                continue
            if not data:
                continue
            ns = time.monotonic_ns()
            binf.write(struct.pack("<QH", ns, len(data)))
            binf.write(data)
            rid = data[0]
            counts[rid] = counts.get(rid, 0) + 1
            if rid == audio_id:
                audio_frames += 1
                if audio_frames % 100 == 0:
                    print(f"   ...{audio_frames} audio frames", file=sys.stderr)
            elif not args.no_enable:
                # consumer report carrying the voice key -> drive the handshake
                if args.voice_key in data[1:3] and not enabled:
                    enabled = True
                    write_enable(0x01)
                elif rid != audio_id and enabled and set(data[1:3]) == {0}:
                    enabled = False
                    write_enable(0x00)
    except KeyboardInterrupt:
        print("\n>> stopping", file=sys.stderr)
    finally:
        if enabled:
            write_enable(0x00)
        os.close(fd)
        binf.close()

    sidecar.write_text(json.dumps({
        "device": dev,
        "mac": args.mac,
        "captured_utc": dt.datetime.now(dt.UTC).isoformat(),
        "audio_report_id": audio_id,
        "enable_report_id": enable_id,
        "report_map": {f"0x{k:02x}": v for k, v in reports.items()},
        "report_id_counts": {f"0x{k:02x}": v for k, v in sorted(counts.items())},
        "audio_frames": audio_frames,
    }, indent=2))
    print(f">> wrote {out} ({audio_frames} audio frames) + {sidecar.name}", file=sys.stderr)
    print(f">> decode: just decode {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
