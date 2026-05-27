#!/usr/bin/env python3
"""Phase 4/5: decode captured HID audio reports (Opus) to a WAV.

Reads a capture produced by 03_capture_voice.py ({ns:u64}{len:u16}{report}),
keeps only the audio report id (from the JSON sidecar, or --audio-report),
strips the 1-byte report id, and decodes each payload as one self-delimited
Opus packet. The Fire remote streams 16 kHz mono, 20 ms frames (TOC 0xB8 =
config 23, CELT wideband), one packet per HID report.

Usage:
    uv run python scripts/04_decode_opus.py captures/voice_<utc>.bin
    uv run python scripts/04_decode_opus.py in.bin --out out.wav --rate 16000 --frame-ms 20
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import wave
from pathlib import Path

import opuslib

HEADER = struct.Struct("<QH")


def read_reports(path: Path):
    """Yield (monotonic_ns, report_bytes) from a 03_capture_voice .bin."""
    data = path.read_bytes()
    off = 0
    while off + HEADER.size <= len(data):
        ns, length = HEADER.unpack_from(data, off)
        off += HEADER.size
        yield ns, data[off:off + length]
        off += length


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="capture .bin from 03_capture_voice.py")
    ap.add_argument("--out", help="output .wav (default alongside input)")
    ap.add_argument("--audio-report", type=lambda s: int(s, 0),
                    help="audio report id (default: read from .json sidecar)")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--channels", type=int, default=1)
    ap.add_argument("--frame-ms", type=float, default=20.0)
    args = ap.parse_args()

    inp = Path(args.input)
    audio_id = args.audio_report
    if audio_id is None:
        sidecar = inp.with_suffix(inp.suffix + ".json")
        if sidecar.exists():
            audio_id = json.loads(sidecar.read_text())["audio_report_id"]
        else:
            raise SystemExit("No sidecar found; pass --audio-report (e.g. 0xf0).")
    print(f">> audio report id = 0x{audio_id:02x}", file=sys.stderr)

    frame_size = int(args.rate * args.frame_ms / 1000)  # samples/channel, e.g. 320
    dec = opuslib.Decoder(args.rate, args.channels)
    pcm = bytearray()
    ok = bad = 0
    tocs = set()
    for _ns, report in read_reports(inp):
        if not report or report[0] != audio_id:
            continue
        payload = report[1:]
        if not payload:
            continue
        tocs.add(payload[0])
        try:
            pcm += dec.decode(bytes(payload), frame_size)
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad += 1
            if bad <= 3:
                print(f"decode err: {e}", file=sys.stderr)

    if ok == 0:
        raise SystemExit("No audio packets decoded — wrong report id or empty capture.")

    out = Path(args.out) if args.out else inp.with_suffix(".wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(args.channels)
        w.setsampwidth(2)
        w.setframerate(args.rate)
        w.writeframes(bytes(pcm))

    secs = len(pcm) / 2 / args.channels / args.rate
    print(f">> decoded ok={ok} bad={bad}  TOC bytes={[hex(t) for t in sorted(tocs)]}")
    print(f">> {secs:.2f}s @ {args.rate}Hz x{args.channels} -> {out}")


if __name__ == "__main__":
    main()
