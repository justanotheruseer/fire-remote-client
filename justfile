# Fire TV Voice Remote BLE reverse-engineering pipeline.
# All paths are relative to the repo root. Run `just` to list targets.
#
# Pairing/capture need elevation for btmon and /dev/hidraw* — scripts use doas
# (Alpine) or sudo (Arch), or run them directly under root.

# Remote BLE MAC — set this to YOUR remote's MAC (from `just pair` or
# `bluetoothctl devices`). Override per-invocation: just MAC=AA:.. capture
MAC := "AA:BB:CC:DD:EE:FF"
ADAPTER := "hci0"
NAME_RE := "amazon|fire|alexa"

_default:
    @just --list

# Phase 1: guided pairing + HCI capture, then extract & print the LTK.
# Idempotent: re-running after bonding just re-extracts the LTK.
pair:
    REMOTE_MAC={{MAC}} ADAPTER={{ADAPTER}} bash scripts/01_pair_and_capture.sh

# Phase 2: dump the GATT tree (services/chars/flags) -> captures/gatt_enum_<utc>.yaml
enum:
    uv run python scripts/02_enum_gatt.py --mac {{MAC}}

# Phase 3: capture voice audio via hidraw (reactive mic handshake).
# Hold the voice button while it runs. Ctrl-C or --duration to stop.
# Writes captures/voice_<utc>.bin (+ .json sidecar). Needs root.
capture:
    doas python3 scripts/03_capture_voice.py --mac {{MAC}}

# Same capture but buttons only (no mic handshake) — for input mapping.
capture-buttons:
    doas python3 scripts/03_capture_voice.py --mac {{MAC}} --no-enable

# Phase 4/5: decode a captured .bin (Opus -> WAV).
decode FILE:
    uv run python scripts/04_decode_opus.py {{FILE}}

# Decode the button presses in a capture into a labeled table.
buttons FILE:
    uv run python scripts/decode_buttons.py {{FILE}}

# All-in-one PoC: live button mapping + reactive voice capture -> WAV.
# Press buttons to see them named; hold voice to record. Ctrl-C to stop.
poc:
    doas python3 scripts/poc_fire_voice.py --mac {{MAC}}
