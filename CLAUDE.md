# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hardware reverse-engineering tool for the **Fire TV Stick 4K Alexa Voice
Remote** over BLE: pair it, capture the microphone audio, decode it to WAV, and
map the buttons. Linux/BlueZ host as central, no sniffer hardware. Validated on
exactly one unit (Amazon vendor `0x0171`, product `0x042F`); report IDs/codec
may differ on other models, so changes should preserve the auto-detection paths
rather than hardcode.

Working on this meaningfully requires the physical remote, a BLE adapter, and
root. Without hardware you can only edit/lint and reason about captures.

## The one architectural insight that matters

The voice audio does **not** ride the vendor GATT services (`5de2…`, `cfbf…`,
`fe1515…`), and you cannot drive it through bleak/GATT:

- bleak can't even connect — the remote uses LE Privacy (resolvable random
  address) and stops advertising once connected, so discovery-based lookup
  fails. `02_enum_gatt.py` therefore reads the GATT tree from **BlueZ over
  D-Bus** (`GetManagedObjects`), not bleak.
- The audio rides **HID-over-GATT** (service `0x1812`). BlueZ's input plugin
  claims that service, so userspace GATT writes to it return "Operation Not
  Authorized". The kernel exposes the remote as a **hidraw** node, and we
  read/write that directly instead.

The mic handshake is **reactive** and lives in `03_capture_voice.py` /
`poc_fire_voice.py`: hold voice → remote emits a consumer report with the voice
key (`0x21`) → only then does writing the 1-byte **output report `0xF2` = `0x01`**
start the stream (cold writes do nothing); `0x00` stops it. Audio arrives as
**input report `0xF0`** (80 B/frame, ~50/s), each payload being one Opus packet
(16 kHz mono, 20 ms, TOC `0xB8`). Report IDs are auto-detected by parsing the
HID report descriptor (`parse_report_descriptor`: largest input = audio,
smallest output = enable) — don't hardcode `0xF0`/`0xF2`.

## Two runtime contexts (important, easy to get wrong)

- **Root + stdlib only** — `01_pair_and_capture.sh`, `03_capture_voice.py`,
  `poc_fire_voice.py`. These need `/dev/hidraw*`/`btmon` (root) and are written
  with no third-party imports so they run under `doas python3 …` *without* the
  venv. `poc_fire_voice.py` is the exception that needs opuslib: it imports it
  from `.venv` via a `sys.path` shim so it still runs under root.
- **venv (no root)** — `02_enum_gatt.py`, `04_decode_opus.py`,
  `decode_buttons.py`. Run via `uv run python …`; these use `dbus-fast` /
  `opuslib` / `pyyaml`.

`doas` strips the environment, so env-var-driven scripts (`01`, which reads
`REMOTE_MAC`/`ADAPTER`) must be run as the **normal user** (it self-elevates
internally), not under `doas`.

## Capture format

`03` logs every HID report as `{monotonic_ns:u64}{len:u16}{bytes}` to
`captures/voice_<utc>.bin` plus a `.json` sidecar (report map, per-id
histogram, detected audio/enable report IDs). `04_decode_opus.py` and
`decode_buttons.py` both consume that `.bin`; `04` reads the audio report id
from the sidecar. The `poc` script does capture+decode in one process instead.

## Commands

```sh
uv sync                                   # install deps (needs libopus: apk add opus / pacman -S opus)
uv run ruff check scripts/                # lint (the only check; there is no test suite)

# Pipeline (just targets wrap these; set MAC in the justfile or pass MAC=..)
REMOTE_MAC=<MAC> bash scripts/01_pair_and_capture.sh   # pair + extract LTK (run as user)
uv run python scripts/02_enum_gatt.py --mac <MAC>      # GATT enum -> captures/gatt_enum_<utc>.yaml
doas python3 scripts/03_capture_voice.py --mac <MAC>   # voice/button capture -> .bin
uv run python scripts/04_decode_opus.py <capture.bin>  # -> WAV
uv run python scripts/decode_buttons.py <capture.bin>  # labeled button table
doas python3 scripts/poc_fire_voice.py --mac <MAC>     # all-in-one: live buttons + voice->WAV
```

`just` targets mirror these: `pair`, `enum`, `capture`, `capture-buttons`,
`decode FILE`, `buttons FILE`, `poc`. The `MAC` in the justfile is a placeholder
— set it to the real remote MAC.

## Safety

The `fe151500-…` vendor service (read/write/notify/indicate) looks like
config/OTA; `02_enum_gatt.py` flags it `danger`. Never write to / fuzz it —
wrong writes to a firmware surface can brick the remote. The audio path never
needs it. Writing `0xF2` (mic enable/disable) is exactly what the stock host
does and is safe.

## Publishing

`captures/*` (`.btsnoop`/`.bin`/`.wav`/`.yaml`/`.json`) are gitignored and must
never be committed — they contain LTK/IRK key material and recorded audio. Keep
the device MAC a placeholder in committed files.
