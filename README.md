# Fire TV Voice Remote — BLE audio reverse engineering

Capture and decode the microphone audio that the Amazon Fire TV Stick 4K Alexa
Voice Remote streams over BLE when the voice button is held — using only the
host's own Bluetooth stack, no sniffer hardware.

**Status: solved.** Capture → decode → `.wav` works, and the buttons are mapped.

> ⚠️ **Tested on two Amazon remotes:** the Fire TV Stick **4K** (`0x0171:0x042F`)
> and **Lite** (`0x0171:0x041C`) Alexa Voice Remotes. The voice protocol and
> codec were identical on both; only the button set / vendor services differ
> (the Lite has no app-shortcut keys and no config/OTA service). See
> [docs/remotes.md](docs/remotes.md). The scripts auto-detect the audio/enable
> report IDs from the HID descriptor, so other models may work too — a PR with a
> new remote's `decode_buttons.py` output + `report_map` is very welcome.

## TL;DR — the protocol

The voice audio does **not** ride the vendor GATT services. It rides
**HID-over-GATT** (service `0x1812`). The kernel claims the HID service, so we
drive it through the remote's **hidraw** node, not via GATT/bleak.

- **Transport:** `/dev/hidrawN` (match by `HID_UNIQ` = the remote's MAC).
- **Mic enable:** write 1-byte **OUTPUT report `0xF2` = `0x01`** (`0x00` to stop).
  Must be **reactive** — a cold enable does nothing. Press voice → remote emits a
  consumer report with the voice key (`0x21`) → *then* write `0xF2 0x01`.
- **Audio data:** **INPUT report `0xF0`**, 80 bytes/frame, ~50 frames/s.
- **Codec:** **Opus, 16 kHz mono, 20 ms frames, ~32 kbps.** Each `0xF0` payload is
  one self-delimited Opus packet; TOC byte is constant `0xB8` (config 23 =
  CELT-only wideband, 20 ms, mono, 1 frame/packet). Decode straight with libopus.
- **Pairing:** LE legacy **Just Works** (`Authenticated=0`), IRK present (LE
  Privacy / rotating address).

This was confirmed against prior art: [`Staars/berry-examples`
`ble/ble_fireRC.be`](https://github.com/Staars/berry-examples/blob/main/ble/ble_fireRC.be)
(same UUIDs, same `0x01` enable, `audio.opus`).

## Button map (this unit: Fire TV Stick 4K remote)

Decoded with `scripts/decode_buttons.py`. Three input reports carry buttons:

| Report | Page | Button → code |
|---|---|---|
| `0x01` | Keyboard (0x07) | d-pad: up `0x52`, down `0x51`, left `0x50`, right `0x4F`; select `0x58`; back `0xF1` |
| `0x02` | Consumer (0x0C), LE 16-bit usage | voice `0x0221`, home `0x0223`, menu `0x0040`, mute `0x00E2`, vol+ `0x00E9`, vol− `0x00EA`, play/pause `0x00CD`, rewind `0x00B4`, fast-fwd `0x00B3`, tv `0x008D` |
| `0xEF` | Vendor input, payload[0] | prime_video `0xA1`, netflix `0xA2`, disney_plus `0xA3`, **peacock `0xA4`** |

(The reference unit had Hulu on `0xA4`; the app-shortcut codes are fixed but the
printed app depends on the remote SKU.)

## Requirements

- Linux host as BLE central; BlueZ 5.x, `bluetoothctl`, `btmon`.
- `uv` (Python) and `libopus` (Arch: `pacman -S opus`, Alpine: `apk add opus`).
- `just` (optional). Elevation for `btmon`/`/dev/hidraw*` — scripts auto-detect
  `doas` (Alpine) / `sudo` (Arch), or run them as root.

```sh
uv sync
```

## Pipeline

| Phase | Target | Script |
|------|--------|--------|
| 1 — pair + HCI capture | `just pair` | `scripts/01_pair_and_capture.sh` |
| 2 — enumerate GATT | `just enum` | `scripts/02_enum_gatt.py` |
| 3 — capture voice | `just capture` | `scripts/03_capture_voice.py` |
| 3b — capture buttons | `just capture-buttons` | `scripts/03_capture_voice.py --no-enable` |
| 4/5 — decode → WAV | `just decode FILE` | `scripts/04_decode_opus.py` |
| — map buttons | `just buttons FILE` | `scripts/decode_buttons.py` |

### Phase 1 — pairing

> The remote bonds to one host at a time. Pairing it here will break its bond to
> any Fire TV (and vice-versa).

`just pair` starts a quiet `btmon` capture, waits for the remote to advertise
(hold **Home ~10 s** to enter pairing mode — these remotes have **no LED**, so
there's no visual indicator; just keep holding), pairs/trusts/connects
non-interactively, then prints the LTK and pairing metadata. Idempotent — re-runs
just re-extract the LTK.

**Note on HCI captures:** `btmon` records at the HCI layer, so ATT in the
`.btsnoop` is **already plaintext** — no LTK import needed to read it in
Wireshark. The LTK matters only if you later add an over-the-air sniffer
(nRF/Ubertooth), where you'd paste it into the nRF Sniffer toolbar (`SC LTK` /
`Legacy LTK`), not into `Preferences → Protocols → BT SMP`.

LTK lives at `/var/lib/bluetooth/<ADAPTER_MAC>/<REMOTE_MAC>/info` under
`[LongTermKey] Key=`. `Authenticated=2` ⇒ LESC; `0` ⇒ legacy (this remote is `0`).

### Phase 3 — capture voice

```sh
just capture                       # uses MAC in justfile; Ctrl-C to stop
# or: doas python3 scripts/03_capture_voice.py --mac <MAC> --duration 15
```

Finds the hidraw node by MAC, parses the HID report descriptor to auto-detect the
audio (`0xF0`) and enable (`0xF2`) report IDs, drives the reactive mic handshake,
and logs **every** report as `{monotonic_ns:u64}{len:u16}{bytes}` to
`captures/voice_<utc>.bin` plus a `.json` sidecar. **Hold the voice button** while
it runs.

### Phase 4/5 — decode

```sh
just decode captures/voice_<utc>.bin     # -> captures/voice_<utc>.wav
```

Reads the audio report id from the sidecar, strips the report-id byte, decodes
each `0xF0` payload as one Opus packet (16 kHz mono, 320-sample frames) → WAV.

## Why hidraw, not bleak/GATT

`bleak` can't attach to this remote: it uses LE Privacy (resolvable random
address) and doesn't advertise while connected, so bleak's discovery-based lookup
fails with `BleakDeviceNotFoundError`. Even when reached over D-Bus, writing the
mic-enable to the HID characteristic returns **"Operation Not Authorized"** —
BlueZ's input plugin owns the HID service. The kernel's hidraw node sidesteps
both: we read `0xF0` input reports and write the `0xF2` output report directly.
(`02_enum_gatt.py` therefore reads the GATT tree via BlueZ's D-Bus
ObjectManager rather than bleak — the remote must be bonded and connected.)

## Hardware seen on this unit

Name `Amazon Remote`, BT vendor `0x0171` (Amazon), product `0x042F`. hidraw
appears as name `AR`, `0005:0171:042F` via `uhid`. (Find your remote's MAC with
`just pair` or `bluetoothctl devices`.)

## Reference projects

- `Staars/berry-examples` `ble/ble_fireRC.be` — same remote, confirmed codec.
- `androidtvremote2` — cousin protocol (pairing + audio split).
- BlueZ `btmon`/`btmgmt` — pairing/encryption flow, `.btsnoop` format.

## License

MIT — see [LICENSE](LICENSE). This is independent reverse-engineering of
hardware the author owns, for interoperability/research; no Amazon SDKs, blobs,
or APKs are used. Not affiliated with or endorsed by Amazon.

## Colophon

The reverse-engineering and tooling here were done with
[Claude Code](https://claude.com/claude-code) (Anthropic) driving the host's
Bluetooth/hidraw stack, with a human pressing the buttons. Prior art that
confirmed the Opus codec: `Staars/berry-examples`.
