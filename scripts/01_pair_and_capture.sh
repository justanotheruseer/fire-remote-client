#!/usr/bin/env bash
# Phase 1: capture the pairing exchange at the HCI level and extract the LTK.
#
# Fully non-interactive: it polls for the remote rather than prompting, so it
# can be driven by a human OR an agent. The only physical step is holding the
# remote's HOME button (~10s) to put it in pairing mode during the scan window.
#
# btmon captures at the HCI layer, so ATT in this .btsnoop is ALREADY PLAINTEXT;
# the LTK is extracted only to confirm the pairing method and for future
# over-the-air sniffing. The LTK is not a one-shot; this script is idempotent.
#
# Usage:
#   REMOTE_MAC=AA:BB:CC:DD:EE:FF bash scripts/01_pair_and_capture.sh
#   SCAN_SECS=90 REMOTE_MAC=.. bash scripts/01_pair_and_capture.sh   # longer window
#   bash scripts/01_pair_and_capture.sh                             # re-extract only
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAPTURES="$REPO_ROOT/captures"
ADAPTER="${ADAPTER:-hci0}"
REMOTE_MAC="${REMOTE_MAC:-}"
NAME_RE="${NAME_RE:-amazon|fire|alexa}"
SCAN_SECS="${SCAN_SECS:-60}"
AGENT="${AGENT:-NoInputNoOutput}"   # Just Works; remote has no keyboard
UTC="$(date -u +%Y%m%dT%H%M%SZ)"
SNOOP="$CAPTURES/pairing_${UTC}.btsnoop"

mkdir -p "$CAPTURES"

# --- elevation: none if already root, else doas (Alpine) / sudo (Arch) ---------
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    ELEV=""
elif command -v doas >/dev/null 2>&1; then
    ELEV=doas
elif command -v sudo >/dev/null 2>&1; then
    ELEV=sudo
else
    echo "ERROR: need root, doas, or sudo to run btmon and read /var/lib/bluetooth." >&2
    exit 1
fi

for bin in btmon bluetoothctl; do
    command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found in PATH." >&2; exit 1; }
done

# --- adapter MAC (sysfs .../address is absent on some kernels; ask BlueZ) -------
ADAPTER_MAC="$(bluetoothctl show 2>/dev/null \
    | awk '/^Controller/ {print toupper($2); exit}')"
if [[ ! "$ADAPTER_MAC" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]]; then
    echo "ERROR: could not resolve adapter MAC (got '$ADAPTER_MAC'). Try: bluetoothctl show" >&2
    exit 1
fi
echo ">> adapter $ADAPTER = $ADAPTER_MAC"

# --- LTK extraction (reusable) -------------------------------------------------
extract_ltk() {
    local mac="${1^^}"
    local info="/var/lib/bluetooth/$ADAPTER_MAC/$mac/info"
    echo
    echo ">> reading $info"
    if ! $ELEV test -f "$info"; then
        echo "ERROR: info file not found — remote is not bonded." >&2
        return 1
    fi
    echo
    echo "============================================================"
    echo "  KEY MATERIAL (secret — do NOT commit)"
    echo "============================================================"
    $ELEV awk '
        /^\[/{ sect=substr($0,2,length($0)-2) }
        /^Key=/ { printf "  [%s] Key=%s\n", sect, substr($0,5) }
    ' "$info"
    echo
    echo ">> pairing metadata:"
    $ELEV awk '
        /^\[/{ sect=substr($0,2,length($0)-2) }
        sect=="LongTermKey" && /^(EncSize|EDiv|Rand|Authenticated)=/ { print "   "$0 }
        /^\[IdentityResolvingKey\]/{ print "   (IRK present -> remote uses LE Privacy / rotating address)" }
        /^\[General\]/{ ingen=1 } ingen && /^Name=/ { print "   "$0; ingen=0 }
    ' "$info"
    echo
    echo "   Authenticated=2 => LE Secure Connections; lower => legacy pairing"
}

# --- helper: is this MAC already bonded? ---------------------------------------
is_bonded() {
    $ELEV test -f "/var/lib/bluetooth/$ADAPTER_MAC/${1^^}/info"
}

# --- bluetoothctl one-shot wrapper ---------------------------------------------
btctl() { bluetoothctl "$@" 2>&1; }

# ==============================================================================
# Path A: already bonded -> just re-extract (idempotent).
# ==============================================================================
if [[ -n "$REMOTE_MAC" ]] && is_bonded "$REMOTE_MAC"; then
    echo ">> $REMOTE_MAC already bonded — skipping pairing, re-extracting LTK."
    extract_ltk "$REMOTE_MAC"
    echo
    echo ">> next: just enum $REMOTE_MAC"
    exit 0
fi

# Resolve a MAC by name if none was pinned and one happens to be bonded already.
if [[ -z "$REMOTE_MAC" ]]; then
    line="$(btctl devices Paired | awk -v re="$NAME_RE" \
        'tolower($0) ~ tolower(re) {print $2; exit}')"
    if [[ -n "$line" ]] && is_bonded "$line"; then
        echo ">> found bonded device matching /$NAME_RE/i: $line — re-extracting."
        extract_ltk "$line"
        exit 0
    fi
    echo "ERROR: no REMOTE_MAC given and no bonded match for /$NAME_RE/i." >&2
    echo "       pass REMOTE_MAC=AA:BB:CC:DD:EE:FF" >&2
    exit 1
fi

# ==============================================================================
# Path B: pair. Start a quiet HCI capture, scan until the remote appears, pair.
# ==============================================================================
echo ">> starting btmon -> $SNOOP (quiet)"
$ELEV btmon -i "$ADAPTER" -w "$SNOOP" >/dev/null 2>&1 &
BTMON_PID=$!
cleanup() { $ELEV kill "$BTMON_PID" 2>/dev/null || true; btctl scan off >/dev/null 2>&1 || true; }
trap cleanup EXIT
sleep 1

# Prep controller + a Just Works agent.
btctl power on        >/dev/null 2>&1 || true
btctl agent "$AGENT"  >/dev/null 2>&1 || true
btctl default-agent   >/dev/null 2>&1 || true

# Forget any stale half-state for this MAC so pairing starts clean.
btctl remove "$REMOTE_MAC" >/dev/null 2>&1 || true

cat <<EOF

============================================================
  HOLD THE REMOTE'S HOME BUTTON NOW (~10s, until RAPID flash)
  Scanning up to ${SCAN_SECS}s for $REMOTE_MAC ...
============================================================
EOF

# Background a timed scan, then poll the device list for our MAC.
bluetoothctl --timeout "$SCAN_SECS" scan on >/dev/null 2>&1 &
SCAN_PID=$!

found=0
for ((i=0; i<SCAN_SECS; i++)); do
    if btctl devices | grep -qi "$REMOTE_MAC"; then
        found=1; echo ">> detected $REMOTE_MAC after ${i}s"; break
    fi
    sleep 1
done
btctl scan off >/dev/null 2>&1 || true
kill "$SCAN_PID" 2>/dev/null || true

if [[ "$found" -ne 1 ]]; then
    echo "ERROR: $REMOTE_MAC never advertised within ${SCAN_SECS}s." >&2
    echo "       Make sure it's in RAPID-flash pairing mode (hold Home ~10s)." >&2
    echo "       If it was paired to a Fire TV, factory-reset it: hold" >&2
    echo "       Left + Back + Menu(☰) together ~12s, then retry." >&2
    exit 1
fi

echo ">> pairing $REMOTE_MAC ..."
pair_out="$(btctl pair "$REMOTE_MAC")"
echo "$pair_out" | sed 's/^/   /'
btctl trust "$REMOTE_MAC"   >/dev/null 2>&1 || true
btctl connect "$REMOTE_MAC" >/dev/null 2>&1 || true
sleep 2

cleanup; trap - EXIT
echo ">> capture saved: $SNOOP"

if is_bonded "$REMOTE_MAC"; then
    echo ">> bond confirmed."
    extract_ltk "$REMOTE_MAC"
    echo
    echo ">> next: just enum $REMOTE_MAC"
else
    echo "ERROR: pairing did not produce a bond. bluetoothctl said:" >&2
    echo "$pair_out" | sed 's/^/   /' >&2
    exit 1
fi
