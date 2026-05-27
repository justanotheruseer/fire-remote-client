#!/usr/bin/env python3
"""Decode button presses from a 03_capture_voice.py capture.

Reads {ns:u64}{len:u16}{report} records, collapses repeats, and prints each
distinct press in time order with its raw bytes and a best-guess label.

HID layout for this Fire remote:
  report 0x01 = keyboard page (d-pad, select); payload[0] = HID keyboard usage
  report 0x02 = consumer page; payload = little-endian 16-bit usage (and app keys)
  report 0xf0 = audio (Opus) — summarized, not listed per-frame

Usage:
    uv run python scripts/decode_buttons.py captures/buttons_<utc>.bin
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

HEADER = struct.Struct("<QH")

# HID Keyboard usage page (0x07) — report 0x01
KEYBOARD = {
    0x4F: "dpad_right", 0x50: "dpad_left", 0x51: "dpad_down", 0x52: "dpad_up",
    0x28: "select", 0x58: "select", 0x29: "back_esc", 0xF1: "back",
}
# Consumer page (0x0C), little-endian 16-bit usage — report 0x02
CONSUMER = {
    0x0221: "voice", 0x0223: "home", 0x0040: "menu", 0x00E2: "mute",
    0x00E9: "volume_up", 0x00EA: "volume_down", 0x00CD: "play_pause",
    0x00B4: "rewind", 0x00B3: "fast_forward", 0x008D: "tv",
    0x0030: "power", 0x0066: "power", 0x0224: "back",
}
# App-shortcut keys — vendor input report 0xEF, payload[0]. a4=Peacock on this unit.
APPS = {0xA1: "prime_video", 0xA2: "netflix", 0xA3: "disney_plus", 0xA4: "peacock"}


def label(rid: int, payload: bytes) -> str:
    if rid == 0xEF and payload:
        return APPS.get(payload[0], f"app_0x{payload[0]:02x}")
    if rid == 0x01 and payload:
        return KEYBOARD.get(payload[0], f"kbd_usage_0x{payload[0]:02x}")
    if rid == 0x02 and payload:
        usage = int.from_bytes(payload[:2], "little")
        if usage in CONSUMER:
            return CONSUMER[usage]
        return f"consumer_usage_0x{usage:04x}"
    return f"report_0x{rid:02x}"


def is_release(payload: bytes) -> bool:
    return set(payload) <= {0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--audio-report", type=lambda s: int(s, 0), default=0xF0)
    args = ap.parse_args()

    data = Path(args.input).read_bytes()
    off = 0
    presses = []
    audio = 0
    t0 = None
    last = None
    while off + HEADER.size <= len(data):
        ns, length = HEADER.unpack_from(data, off)
        off += HEADER.size
        rec = data[off:off + length]
        off += length
        if not rec:
            continue
        rid, payload = rec[0], rec[1:]
        if rid == args.audio_report:
            audio += 1
            continue
        if t0 is None:
            t0 = ns
        if is_release(payload):
            last = None
            continue
        key = (rid, bytes(payload))
        if key == last:
            continue  # held/repeat
        last = key
        presses.append(((ns - t0) / 1e9, rid, payload))

    print(f"{'t(s)':>7}  {'report':6}  {'raw':14}  label")
    print("-" * 48)
    for t, rid, payload in presses:
        print(f"{t:7.2f}  0x{rid:02x}    {payload.hex():14}  {label(rid, payload)}")
    print(f"\n{len(presses)} distinct presses; {audio} audio (0x{args.audio_report:02x}) frames "
          f"({audio*0.02:.1f}s)")


if __name__ == "__main__":
    main()
