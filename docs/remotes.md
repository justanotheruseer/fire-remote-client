# Tested remotes

Per-device results. The capture/decode pipeline auto-detects the audio/enable
HID report IDs from the report descriptor, so it has worked unmodified on every
remote tried so far. Differences below are about *which* buttons/services exist,
not about the core voice protocol.

| | Fire TV Stick 4K remote | Fire TV Stick Lite remote |
|---|---|---|
| BT vendor:product | `0x0171:0x042F` | `0x0171:0x041C` |
| hidraw name | `AR` | `AR` |
| Pairing | LE legacy Just Works, IRK | LE legacy Just Works, IRK |
| Voice service `5de2…` | ✓ | ✓ |
| `cfbf…` service | ✓ | ✓ |
| `fe1515…` (config/OTA) | ✓ | **absent** |
| Audio report / enable report | `0xF0` / `0xF2` | `0xF0` / `0xF2` |
| Codec | Opus 16 kHz mono 20 ms | Opus 16 kHz mono 20 ms |
| App-shortcut buttons (`0xEF`) | prime/netflix/disney/peacock | **none** (report defined, no keys) |

Both share an identical HID report map: `0x01` keyboard (d-pad/select/back),
`0x02` consumer (home/menu/mute/play/etc.), `0xEF` vendor app keys, `0xF0`
80-byte audio input, `0xF2` 1-byte mic-enable output, plus `0xF1`/`0xF3`/`0x03`.

## Button codes

See the table in the [README](../README.md#button-map-this-unit-fire-tv-stick-4k-remote).
The Lite uses the same keyboard/consumer codes; it just omits the app-shortcut
row and (depending on SKU) some media keys.

## Contributing another remote

If you have a different Fire remote, a PR adding a row here is welcome. Capture
it and paste:

```sh
doas python3 scripts/03_capture_voice.py --mac <MAC> --no-enable --duration 30
# press every button once, then:
uv run python scripts/decode_buttons.py captures/voice_<utc>.bin
```

Include the `report_map` and `audio_report_id`/`enable_report_id` from the
capture's `.json` sidecar, the `decode_buttons.py` table, and the BT
vendor:product (from `/var/lib/bluetooth/.../info` `[DeviceID]`).
