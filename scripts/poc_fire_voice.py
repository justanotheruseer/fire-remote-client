#!/usr/bin/env python3
"""All-in-one PoC for the Fire TV Stick 4K voice remote.

Does everything in one process:
  * finds the remote's hidraw node by MAC and auto-detects its report IDs
  * prints every button press live, with its decoded name + raw bytes
  * drives the reactive mic handshake (write 0xF2=0x01 after the voice key)
  * collects the 0xF0 Opus stream and, on exit, decodes it to a .wav

Run as root (hidraw access). Pure-stdlib except opuslib, which it imports from
the project .venv automatically (opuslib is pure-python + ctypes to libopus).

    doas python3 scripts/poc_fire_voice.py --mac AA:BB:CC:DD:EE:FF
    doas python3 scripts/poc_fire_voice.py --mac <MAC> --duration 20 --out demo

Press buttons to see them mapped; hold the voice button to record. Ctrl-C to
stop and write the WAV.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import select
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES = REPO_ROOT / "captures"
SYS_HIDRAW = "/sys/class/hidraw"


def _import_opuslib():
    try:
        import opuslib  # noqa: PLC0415
        return opuslib
    except ImportError:
        for sp in glob.glob(str(REPO_ROOT / ".venv/lib/python*/site-packages")):
            sys.path.insert(0, sp)
        try:
            import opuslib  # noqa: PLC0415
            return opuslib
        except ImportError as e:
            raise SystemExit("opuslib not found — run `uv sync` first.") from e


# ---- button maps (Fire TV Stick 4K remote) -----------------------------------
KEYBOARD = {  # report 0x01, payload[0] = HID keyboard usage
    0x4F: "dpad_right", 0x50: "dpad_left", 0x51: "dpad_down", 0x52: "dpad_up",
    0x58: "select", 0x28: "select", 0xF1: "back",
}
CONSUMER = {  # report 0x02, little-endian 16-bit consumer usage
    0x0221: "voice", 0x0223: "home", 0x0040: "menu", 0x00E2: "mute",
    0x00E9: "volume_up", 0x00EA: "volume_down", 0x00CD: "play_pause",
    0x00B4: "rewind", 0x00B3: "fast_forward", 0x008D: "tv", 0x0224: "back",
}
APPS = {  # report 0xEF, payload[0] (a4 = Peacock on this SKU)
    0xA1: "prime_video", 0xA2: "netflix", 0xA3: "disney_plus", 0xA4: "peacock",
}
VOICE_KEY = 0x21  # consumer usage low byte for the mic button


def button_name(rid: int, payload: bytes) -> str | None:
    if rid == 0xEF and payload:
        return APPS.get(payload[0], f"app_0x{payload[0]:02x}")
    if rid == 0x01 and payload:
        return KEYBOARD.get(payload[0], f"kbd_0x{payload[0]:02x}")
    if rid == 0x02 and payload:
        u = int.from_bytes(payload[:2], "little")
        return CONSUMER.get(u, f"consumer_0x{u:04x}")
    return None


# ---- HID report-descriptor parse (find audio + enable report ids) -------------
def parse_reports(buf: bytes) -> dict[int, dict]:
    reports: dict[int, dict] = {}
    rid = rsize = rcount = 0
    i = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        if b == 0xFE:
            if i < len(buf):
                i += 2 + buf[i]
            continue
        tag, typ, size = b >> 4, (b >> 2) & 3, b & 3
        nb = (0, 1, 2, 4)[size]
        data = int.from_bytes(buf[i:i + nb], "little") if nb else 0
        i += nb
        if typ == 1:  # Global: Report Size / Count / ID
            if tag == 0x7:
                rsize = data
            elif tag == 0x9:
                rcount = data
            elif tag == 0x8:
                rid = data
        elif typ == 0 and tag in (0x8, 0x9):  # Main: Input / Output
            d = "input" if tag == 0x8 else "output"
            reports.setdefault(rid, {"input": 0, "output": 0})[d] += rcount * rsize // 8
    return reports


def find_hidraw(mac: str) -> str:
    mac = mac.lower()
    for path in glob.glob(f"{SYS_HIDRAW}/hidraw*"):
        try:
            ue = Path(path, "device", "uevent").read_text().lower()
        except OSError:
            continue
        if f"hid_uniq={mac}" in ue:
            return f"/dev/{os.path.basename(path)}"
    raise SystemExit(f"No hidraw node for {mac} — wake/connect the remote and retry.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--out", help="basename for outputs (default captures/poc_<utc>)")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--frame-ms", type=float, default=20.0)
    args = ap.parse_args()

    opuslib = _import_opuslib()
    dev = find_hidraw(args.mac)
    rdesc = Path(SYS_HIDRAW, os.path.basename(dev), "device", "report_descriptor").read_bytes()
    reports = parse_reports(rdesc)
    inputs = {r: v["input"] for r, v in reports.items() if v.get("input")}
    outputs = {r: v["output"] for r, v in reports.items() if v.get("output")}
    audio_id = max(inputs, key=inputs.get)
    enable_id = min(outputs, key=outputs.get)
    print(f">> {dev}  audio=0x{audio_id:02x}  enable=0x{enable_id:02x}  "
          f"({inputs[audio_id]}B/frame)")

    CAPTURES.mkdir(exist_ok=True)
    base = Path(args.out) if args.out else \
        CAPTURES / f"poc_{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}"
    if base.parent == Path("."):
        base = CAPTURES / base

    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    packets: list[bytes] = []
    presses: list[tuple[float, str]] = []
    enabled = False
    last_key = None
    t0 = time.monotonic()

    def enable(val: int) -> None:
        try:
            os.write(fd, bytes([enable_id, val]))
        except OSError as e:
            print(f"!! enable {val:#04x}: {e}")

    print(">> running — press buttons / hold the voice button. Ctrl-C to stop.\n")
    deadline = t0 + args.duration if args.duration else None
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
            rid, payload = data[0], data[1:]
            t = time.monotonic() - t0

            if rid == audio_id:
                packets.append(bytes(payload))
                continue

            released = set(payload) <= {0}
            if released:
                last_key = None
                if enabled:  # voice button up -> stop mic
                    enabled = False
                    enable(0x00)
                    print(f"[{t:6.2f}] voice ↑ (mic off)")
                continue

            # mic handshake: voice key down -> enable
            if rid == 0x02 and VOICE_KEY in payload[:2] and not enabled:
                enabled = True
                enable(0x01)

            key = (rid, bytes(payload))
            if key == last_key:
                continue  # holding / autorepeat
            last_key = key
            name = button_name(rid, payload) or f"report_0x{rid:02x}"
            presses.append((t, name))
            print(f"[{t:6.2f}] {name:16} (report 0x{rid:02x}, raw {payload.hex()})")
    except KeyboardInterrupt:
        print("\n>> stopping")
    finally:
        if enabled:
            enable(0x00)
        os.close(fd)

    # ---- decode the Opus stream -> WAV ----
    wav = base.with_suffix(".wav")
    frame_size = int(args.rate * args.frame_ms / 1000)
    dec = opuslib.Decoder(args.rate, 1)
    pcm = bytearray()
    ok = bad = 0
    for p in packets:
        if not p:
            continue
        try:
            pcm += dec.decode(p, frame_size)
            ok += 1
        except Exception:  # noqa: BLE001
            bad += 1

    print("\n==================== SUMMARY ====================")
    print(f"buttons pressed: {len(presses)}")
    for t, name in presses:
        print(f"   {t:6.2f}s  {name}")
    print(f"\naudio: {len(packets)} Opus packets, decoded ok={ok} bad={bad}")
    if ok:
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(args.rate)
            w.writeframes(bytes(pcm))
        secs = len(pcm) / 2 / args.rate
        print(f"wrote {wav}  ({secs:.2f}s, {args.rate}Hz mono)")
        print(f"play:  aplay {wav}")
    else:
        print("no audio captured (did you hold the voice button?)")


if __name__ == "__main__":
    main()
