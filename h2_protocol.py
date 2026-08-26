#!/usr/bin/env python3
"""
h2_protocol.py — shared HID protocol layer for the EWEADN H2 mouse.

Everything here is imported by both h2_battery.py (CLI) and
h2_gui_qt.py (GUI) — device discovery, the low-level read/write
plumbing, DPI encode/decode, and every device command (battery/info,
polling rate, DPI table, button bindings). Keeping this in one place
means a protocol fix (or a newly reverse-engineered command) only has
to be made once and both front-ends pick it up.

Protocol reverse-engineered from the official web driver
(hub.eweadn.cn/pq/h2) — specifically getDeviceInfo(), getDpiData(),
setDpiValue(), getReportRate(), setReportRate(), getMouseKeys(), and
setMouseKeys() in the driver's JS, plus the v()/k() DPI value
encode/decode helpers, the setData() checksum wrapper shared by every
command, and the driver's static keycode table (the `eb` JSON blob in
page-f3c2019a603488ce.js) for the extended button-binding types.

Requires: pip install hidapi --break-system-packages
Requires read/write permission on the device's hidraw node — add a
udev rule (see README notes) so callers don't need sudo.

This module has no CLI/GUI of its own — see h2_battery.py and
h2_gui_qt.py for those.
"""

import sys
import time

try:
    import hid
except ImportError:
    print("Missing dependency. Install with: pip install hidapi --break-system-packages", file=sys.stderr)
    sys.exit(1)

VENDOR_ID = 0x089D   # 2205 — wireless (2.4G dongle) mode
PRODUCT_ID = 0x062F  # 1583

# The mouse presents a DIFFERENT USB identity while connected via cable
# (which is also the mode it's in while charging) — the dongle's
# vendor-command channel appears to stop responding once wired charging
# is active, even though the dongle's HID interfaces are still enumerated.
WIRED_VENDOR_ID = 0x088D
WIRED_PRODUCT_ID = 0x062E

# Tried in order — wired first, since that's the identity active while
# charging (the main scenario this was added for). Falls through to the
# wireless dongle identity if the wired one isn't present.
KNOWN_DEVICE_IDS = [
    (WIRED_VENDOR_ID, WIRED_PRODUCT_ID),
    (VENDOR_ID, PRODUCT_ID),
]

READ_TIMEOUT_MS = 1000

# USB/dongle polling rate <-> device register code, taken directly from
# the driver's "usbReportRate"/"dongleReportRate" option lists.
REPORT_RATE_TO_CODE = {125: 8, 250: 4, 500: 2, 1000: 1}
CODE_TO_REPORT_RATE = {v: k for k, v in REPORT_RATE_TO_CODE.items()}

DPI_SLOT_COUNT = 6

ASYNC_STATUS_MARKERS = (209, 210)  # unsolicited status pushes, not command replies


# ---------------------------------------------------------------------------
# Low-level HID plumbing
# ---------------------------------------------------------------------------

def find_interface_path():
    """
    The H2 exposes 3 HID interfaces (mouse, keyboard, vendor-control).
    The vendor/config interface is the one the web driver talks to
    (interface number 2 in the lsusb -v output for the wireless
    identity — NOTE: this hasn't been independently confirmed for the
    wired identity's interface layout, since lsusb -v -d 088d:062e
    output hasn't been captured yet. If reads still fail while wired,
    that's the next thing to check).

    Tries each known device identity in order (wired first, since
    that's the one active while charging) and returns the first
    matching interface found.
    """
    for vendor_id, product_id in KNOWN_DEVICE_IDS:
        candidates = hid.enumerate(vendor_id, product_id)
        if not candidates:
            continue

        for c in candidates:
            if c.get("interface_number") == 2:
                return c["path"]

        # Fallback for hidapi backends that don't report interface_number
        # reliably, or if this identity's vendor interface isn't at index 2.
        return candidates[-1]["path"]

    return None


def open_device():
    path = find_interface_path()
    if path is None:
        raise RuntimeError(
            "EWEADN H2 not found (checked both wireless-dongle and wired-charging "
            "USB identities) — is it connected (2.4G dongle, wired cable, or BT)?"
        )
    dev = hid.device()
    dev.open_path(path)
    return dev


def drain_stale_reports(dev, max_drain=20):
    """
    Flush any reports the device already pushed before we asked for
    anything — leftover heartbeat/status packets sitting in the kernel's
    read buffer would otherwise get returned instead of our actual
    command's response (this is why a fresh open can read back stale,
    e.g. battery=0, data on the first poll).
    """
    dev.set_nonblocking(True)
    for _ in range(max_drain):
        if not dev.read(64):
            break
    dev.set_nonblocking(False)


def checksum(arr):
    """Mirrors setData(): sum of bytes[5:32], masked to a byte."""
    return sum(arr[5:32]) & 0xFF


def send_write_command(dev, arr, settle_delay=0.3):
    """
    Send a SET/write command. Unlike send_and_read(), this never
    resends the same command automatically — retrying a write blindly
    risks landing a second write while the device is still committing
    the first one to flash/EEPROM, which can corrupt its stored config
    (this is what happened during testing: a resent DPI write left the
    device with impossible values like dpi_count=13).

    We still read once for an ack (skipping async status packets), but
    a missing/short ack is only reported to the caller — never used as
    a signal to resend. settle_delay gives the device time to finish
    committing before any follow-up command touches it.
    """
    arr = list(arr)
    arr[32] = checksum(arr)

    drain_stale_reports(dev)
    dev.write(arr)

    resp = None
    for _ in range(5):
        candidate = dev.read(33, timeout_ms=READ_TIMEOUT_MS)
        if not candidate:
            break
        if candidate[0] in ASYNC_STATUS_MARKERS:
            continue
        resp = candidate
        break

    time.sleep(settle_delay)
    return resp  # caller decides whether a missing ack is fatal


def send_and_read(dev, arr, min_resp_len=5, retry_on=None, attempts=3):
    """
    Send a 33-byte command array (checksum filled in automatically) and
    read the 33-byte response. retry_on(resp) can be given to detect a
    stray/stale packet (same idea as the report_max check in
    get_battery_info) and retry.

    Two kinds of noise get filtered out here:
      - Empty/short reads (the 2.4G dongle occasionally drops or delays
        a reply outright) — retried up to `attempts` times with backoff.
      - Unsolicited async status packets (first byte 209/210 — the
        device pushes these independently of command replies, e.g. on
        connect/charge-state changes) — skipped in favor of the actual
        reply, which may arrive in the next read.
    """
    arr = list(arr)
    arr[32] = checksum(arr)

    last_resp = None
    for attempt in range(attempts):
        drain_stale_reports(dev)
        dev.write(arr)

        resp = None
        for _ in range(5):  # a few status packets could be queued ahead of our reply
            candidate = dev.read(33, timeout_ms=READ_TIMEOUT_MS)
            if not candidate:
                break
            if candidate[0] in ASYNC_STATUS_MARKERS:
                continue  # not our reply — keep waiting for the real one
            resp = candidate
            break
        last_resp = resp

        if resp and len(resp) >= min_resp_len:
            if retry_on is None or not retry_on(resp):
                return resp
            # stale/implausible data — fall through and retry

        if attempt < attempts - 1:
            time.sleep(0.15 * (attempt + 1))  # brief backoff before retrying

    raise RuntimeError(f"Unexpected/empty response after {attempts} attempts: {last_resp}")


# ---------------------------------------------------------------------------
# DPI value encode/decode (mirrors the driver's v()/k() helpers)
# ---------------------------------------------------------------------------

def encode_dpi(value, ic_type=17):
    """
    Convert a real DPI number into the device's register value.
    Only ic_type 17 is implemented — that's what this H2 unit reports
    (confirmed via --get-config / the battery read). Other ic_types use
    different breakpoints in the driver and aren't needed here.
    """
    if ic_type != 17:
        raise NotImplementedError(f"DPI encoding only implemented for ic_type 17 (got {ic_type})")
    if value >= 13000:
        return ((value - 13000) // 1000) + 221
    elif value > 10000:
        return ((value - 10000) // 100) + 200
    else:
        return value // 50


def decode_dpi(raw, ic_type=17):
    """Inverse of encode_dpi — used when reading the current DPI config back."""
    if ic_type != 17:
        raise NotImplementedError(f"DPI decoding only implemented for ic_type 17 (got {ic_type})")
    if raw < 200:
        return 50 * raw
    elif raw <= 220:
        return (raw - 200) * 100 + 10000
    else:
        return (raw - 220) * 1000 + 12000


def _lo(v):
    return v & 0xFF


def _hi(v):
    return (v >> 8) & 0xFF


def _combine(lo, hi):
    return (hi << 8) | lo


# ---------------------------------------------------------------------------
# Commands — device info / battery
# ---------------------------------------------------------------------------

def _read_device_info(dev):
    arr = [0x00, 0x30] + [0x00] * 31  # cmd 0x30 = getDeviceInfo
    return send_and_read(
        dev, arr, min_resp_len=15,
        retry_on=lambda r: r[11] == 0 or r[10] == 0,  # report_max or ic_type looking wrong
    )


def get_battery_info(dev):
    resp = _read_device_info(dev)

    device_id = "".join(chr(b) for b in resp[4:8] if b != 0)
    fw_hi = format(resp[9], "X")
    fw_lo = format(resp[8], "X").zfill(2)

    return {
        "device_id": device_id,
        "firmware_version": f"v{fw_hi[0]}.{fw_lo[-2:]}",
        "ic_type": resp[10],
        "report_max": resp[11],
        "charge_flag": resp[12],
        "battery_value": resp[13],
        "connect_status": resp[14],
    }


# ---------------------------------------------------------------------------
# Commands — polling (report) rate
# ---------------------------------------------------------------------------

def get_report_rate(dev):
    arr = [0x00, 18] + [0x00] * 31  # cmd 0x12 = getReportRate
    resp = send_and_read(
        dev, arr,
        retry_on=lambda r: r[4] not in CODE_TO_REPORT_RATE,  # only 1/2/4/8 are valid codes
    )
    code = resp[4]
    return CODE_TO_REPORT_RATE.get(code, code)


def set_report_rate(dev, hz):
    if hz not in REPORT_RATE_TO_CODE:
        raise ValueError(f"Unsupported rate {hz}Hz. Supported: {sorted(REPORT_RATE_TO_CODE)}")
    arr = [0x00] * 33
    arr[1] = 2  # cmd 0x02 = setReportRate
    arr[3] = 1
    arr[4] = 1
    arr[5] = REPORT_RATE_TO_CODE[hz]
    resp = send_write_command(dev, arr)
    if resp is None:
        print("[warn] no ack received for set_report_rate — verify with a read-back before trusting the change", file=sys.stderr)
    return resp


# ---------------------------------------------------------------------------
# Commands — DPI table
# ---------------------------------------------------------------------------

def get_dpi_config(dev, ic_type=17):
    arr = [0x00, 19] + [0x00] * 31  # cmd 0x13 = getDpiData
    slot_offsets = [5, 9, 13, 17, 21, 25]

    def looks_wrong(r):
        # A real reply should have at least the active slot's value
        # non-zero, and dpi_count/dpi_index need to be in sane ranges
        # (1-6 slots). This also catches cross-contamination from a
        # leftover getDeviceInfo reply landing here by accident — that
        # packet's byte 4 is part of the device_id string, which when
        # split into nibbles produces implausible count/index values.
        count = r[4] & 0x0F
        index = (r[4] >> 4) & 0x0F
        if not (1 <= count <= DPI_SLOT_COUNT) or index >= DPI_SLOT_COUNT:
            return True
        return all(r[o] == 0 and r[o + 1] == 0 for o in slot_offsets)

    resp = send_and_read(dev, arr, retry_on=looks_wrong)

    dpi_index = (resp[4] >> 4) & 0x0F
    dpi_count = resp[4] & 0x0F

    levels = [decode_dpi(_combine(resp[o], resp[o + 1]), ic_type) for o in slot_offsets]

    return {
        "dpi_index": dpi_index,   # 0-based active slot
        "dpi_count": dpi_count,
        "levels": levels,        # [dpi1_value, ..., dpi6_value]
    }


def set_dpi_config(dev, levels, dpi_index, dpi_count, ic_type=17):
    """
    Writes all 6 DPI slots at once (the device's protocol has no
    single-slot write — setDpiValue always sends the full table).
    Use set_single_dpi()/set_active_dpi() below to change just one
    thing while preserving the rest of the current config.
    """
    if len(levels) != DPI_SLOT_COUNT:
        raise ValueError(f"levels must have exactly {DPI_SLOT_COUNT} entries")

    arr = [0x00] * 33
    arr[1] = 3  # cmd 0x03 = setDpiValue
    arr[3] = 1
    arr[4] = 26
    arr[6] = (dpi_count & 0x0F) | ((dpi_index & 0x0F) << 4)

    offset = 7
    for value in levels:
        enc = encode_dpi(value, ic_type)
        lo, hi = _lo(enc), _hi(enc)
        # X and Y sensitivity are written as duplicate pairs, matching
        # the driver (it encodes the same value twice per slot).
        arr[offset:offset + 4] = [lo, hi, lo, hi]
        offset += 4

    resp = send_write_command(dev, arr)
    if resp is None:
        print("[warn] no ack received for set_dpi_config — verify with a read-back before trusting the change", file=sys.stderr)
    return resp


def set_single_dpi(dev, slot, value, ic_type=17):
    """slot is 1-6 (matches the driver's dpi1_value..dpi6_value numbering)."""
    if not (1 <= slot <= DPI_SLOT_COUNT):
        raise ValueError(f"slot must be 1-{DPI_SLOT_COUNT}")
    cfg = get_dpi_config(dev, ic_type)
    levels = cfg["levels"]
    levels[slot - 1] = value
    set_dpi_config(dev, levels, cfg["dpi_index"], cfg["dpi_count"], ic_type)


def set_active_dpi(dev, slot, ic_type=17):
    """Switches which DPI slot is currently active, without changing any values."""
    if not (1 <= slot <= DPI_SLOT_COUNT):
        raise ValueError(f"slot must be 1-{DPI_SLOT_COUNT}")
    cfg = get_dpi_config(dev, ic_type)
    set_dpi_config(dev, cfg["levels"], slot - 1, cfg["dpi_count"], ic_type)


def set_dpi_count(dev, count, ic_type=17):
    """
    Sets how many DPI slots are in rotation (cycled through by the
    mouse's onboard DPI button), without changing any slot values or
    which slot is active. Mainly a recovery tool — count is normally
    left alone by set_single_dpi()/set_active_dpi(), which both
    preserve whatever the current count already is.
    """
    if not (1 <= count <= DPI_SLOT_COUNT):
        raise ValueError(f"count must be 1-{DPI_SLOT_COUNT}")
    cfg = get_dpi_config(dev, ic_type)
    set_dpi_config(dev, cfg["levels"], cfg["dpi_index"], count, ic_type)


# ---------------------------------------------------------------------------
# Button remapping
#
# Reverse-engineered from getMouseKeys()/setMouseKeys()/resetAllMouseKeys()
# in the driver's JS. Types 32 (standard mouse button), 64 (double-click/
# fire), 80 (DPI cycle), and 144 (media key) are implemented, using the
# exact code1/code2 values from the driver's static keycode table (the
# `eb` JSON blob in page-f3c2019a603488ce.js — referenced in the JSX as
# eb.B.p). Type 128 (keyboard shortcut) is also implemented: its
# code1/code2 aren't looked up in a table but *computed*, mirroring the
# UI's modifier-checkbox handler exactly (code1 = ctrl|shift<<1|alt<<2|
# win<<3 bitmask, code2 = a standard USB HID keyboard usage ID — public
# spec, not device-specific). Every entry in the table has code3=0,
# confirming the 3-byte (type, code1, code2) slot layout below is
# complete for all of these; code3 only matters for the vestigial,
# unused setMouseKey() (singular) path.
#
# Macro bindings (type 160) are deliberately NOT implemented — writing
# one needs a second command (setMouseMacro) built by an unresolved
# packer function, and getting that wrong risks corrupting macro
# storage the same way a bad DPI write once corrupted dpi_count.
# ---------------------------------------------------------------------------

BUTTON_SLOT_COUNT = 6
MOUSE_BUTTON_TYPE = 32  # type value for "standard mouse button" bindings

# code1 bitmask values seen as factory defaults for a standard mouse
# button binding — matches the standard USB HID mouse button convention.
MOUSE_BUTTON_LEFT = 1
MOUSE_BUTTON_RIGHT = 2
MOUSE_BUTTON_MIDDLE = 4
MOUSE_BUTTON_BACK = 8
MOUSE_BUTTON_FORWARD = 16

# type 80 — DPI cycle button. code2 is always 0.
DPI_CYCLE_TYPE = 80
DPI_CYCLE_CODES = {
    "loop": 1,  # DPI Loop+ — cycle through the active DPI rotation
    "up": 2,    # DPI+ — step to the next-higher slot
    "down": 3,  # DPI- — step to the next-lower slot
}

# type 64 — "Fire Key" group. code1 is always 100; code2 selects the mode.
FIRE_KEY_TYPE = 64
FIRE_KEY_CODES = {
    "doubleclick": 2,  # "Double click left"
    "fire": 3,         # "Fire Key" — rapid-fire click
}

# type 144 — media/consumer keys. (code1, code2) pairs, straight from
# the eb.B.p "media" group — raw HID consumer-usage codes, not computed.
MEDIA_KEY_TYPE = 144
MEDIA_KEY_CODES = {
    "volume_up": (233, 0),
    "volume_down": (234, 0),
    "mute": (226, 0),
    "play_pause": (205, 0),
    "stop": (183, 0),
    "prev_track": (182, 0),
    "next_track": (181, 0),
    "multimedia": (131, 1),
    "homepage": (35, 2),
    "web_refresh": (39, 2),
    "web_stop": (38, 2),
    "web_forward": (37, 2),
    "web_backward": (36, 2),
    "web_favorites": (42, 2),
    "web_search": (33, 2),
    "calculator": (146, 1),
    "my_computer": (148, 1),
    "mail": (138, 1),
}

# type 128 — keyboard shortcut. code1 is a *computed* modifier bitmask
# (this exact bit layout, straight from the UI's modifier-toggle
# handler — not the static table, which only holds a few Ctrl+key
# presets): code2 is a standard USB HID keyboard usage ID (public spec,
# not device-specific). KEY_NAME_TO_HID below maps friendly names (the
# ones a regular user would type) to those IDs so nobody has to look up
# raw usage-table numbers by hand.
KEYBOARD_SHORTCUT_TYPE = 128
MODIFIER_BITS = {
    "ctrl": 1,
    "shift": 2,
    "alt": 4,
    "win": 8,
}

# Standard USB HID keyboard/keypad usage IDs (page 0x07 of the HID Usage
# Tables spec) — fixed, public, and identical on every USB keyboard.
# Not reverse-engineered from the mouse; included here purely so
# callers can take a key name instead of a raw number.
KEY_NAME_TO_HID = {
    **{chr(c): 4 + (c - ord('a')) for c in range(ord('a'), ord('z') + 1)},  # a-z -> 4-29
    "1": 30, "2": 31, "3": 32, "4": 33, "5": 34,
    "6": 35, "7": 36, "8": 37, "9": 38, "0": 39,
    "enter": 40, "return": 40,
    "esc": 41, "escape": 41,
    "backspace": 42,
    "tab": 43,
    "space": 44, "spacebar": 44,
    "minus": 45, "-": 45,
    "equal": 46, "=": 46,
    "leftbracket": 47, "[": 47,
    "rightbracket": 48, "]": 48,
    "backslash": 49, "\\": 49,
    "semicolon": 51, ";": 51,
    "quote": 52, "'": 52, "apostrophe": 52,
    "grave": 53, "`": 53, "tilde": 53,
    "comma": 54, ",": 54,
    "period": 55, ".": 55,
    "slash": 56, "/": 56,
    "capslock": 57,
    **{f"f{n}": 58 + (n - 1) for n in range(1, 13)},   # f1-f12  -> 58-69
    "printscreen": 70,
    "scrolllock": 71,
    "pause": 72,
    "insert": 73,
    "home": 74,
    "pageup": 75,
    "delete": 76, "del": 76,
    "end": 77,
    "pagedown": 78,
    "right": 79,
    "left": 80,
    "down": 81,
    "up": 82,
    "numlock": 83,
    **{f"f{n}": 104 + (n - 13) for n in range(13, 25)},  # f13-f24 -> 104-115
}
HID_TO_KEY_NAME = {v: k for k, v in KEY_NAME_TO_HID.items()}
# KEY_NAME_TO_HID has multiple aliases for a few keys (e.g. "return" is
# an alias for "enter", both map to HID id 40); the naive reversal above
# keeps whichever alias was inserted last, so pin the friendlier name
# explicitly for anything that matters when displaying bindings back.
HID_TO_KEY_NAME.update({40: "enter", 41: "esc", 44: "space", 52: "quote",
                         53: "grave", 76: "delete"})


def parse_modifiers(spec):
    """
    Parses a "ctrl+shift" style string into the code1 bitmask used by
    type-128 bindings. Raises on unknown modifier names rather than
    silently ignoring them — sending a wrong-but-valid-looking bitmask
    would bind the wrong shortcut with no indication anything was off.
    """
    bitmask = 0
    for name in spec.lower().split("+"):
        name = name.strip()
        if name not in MODIFIER_BITS:
            raise ValueError(f"Unknown modifier '{name}'. Valid: {sorted(MODIFIER_BITS)}")
        bitmask |= MODIFIER_BITS[name]
    return bitmask


def parse_key_name(name):
    """
    Converts a friendly key name (e.g. "c", "enter", "f5") into its
    USB HID keyboard usage ID. Raises on unknown names — a silently
    wrong keycode would bind a shortcut to the wrong key with nothing
    to indicate anything was off.
    """
    key = name.strip().lower()
    if key not in KEY_NAME_TO_HID:
        raise ValueError(f"Unknown key '{name}'. Valid: {sorted(KEY_NAME_TO_HID)}")
    return KEY_NAME_TO_HID[key]


def hid_to_key_name(code2):
    """Inverse of parse_key_name, for display. Falls back to the raw code if unrecognized."""
    return HID_TO_KEY_NAME.get(code2, f"HID code {code2}")


# Factory-default binding for all 6 slots, taken directly from
# resetAllMouseKeys()'s hardcoded array. Slot 6's type=80/code1=1 is
# "DPI Loop+" — confirmed against the driver's static keycode table
# (see DPI_CYCLE_CODES above), not just inferred from context anymore.
DEFAULT_BUTTON_BINDINGS = [
    (MOUSE_BUTTON_TYPE, MOUSE_BUTTON_LEFT, 0),
    (MOUSE_BUTTON_TYPE, MOUSE_BUTTON_RIGHT, 0),
    (MOUSE_BUTTON_TYPE, MOUSE_BUTTON_MIDDLE, 0),
    (MOUSE_BUTTON_TYPE, MOUSE_BUTTON_BACK, 0),
    (MOUSE_BUTTON_TYPE, MOUSE_BUTTON_FORWARD, 0),
    (DPI_CYCLE_TYPE, 1, 0),  # DPI Loop+ — confirmed, see DPI_CYCLE_CODES
]


KNOWN_BUTTON_TYPES = {0, MOUSE_BUTTON_TYPE, 50, FIRE_KEY_TYPE, DPI_CYCLE_TYPE, KEYBOARD_SHORTCUT_TYPE, MEDIA_KEY_TYPE, 160}


def get_button_bindings(dev):
    """Returns a list of 6 (type, code1, code2) tuples, one per button slot."""
    arr = [0x00, 17] + [0x00] * 31  # cmd 0x11 = getMouseKeys

    def looks_wrong(r):
        # A genuine getMouseKeys reply should have all 6 slot "type"
        # bytes drawn from the known set. If any slot's type falls
        # outside that set, this is very likely a stale/mismatched
        # reply from a DIFFERENT command (e.g. getDeviceInfo) caught
        # instead of ours.
        for i in range(BUTTON_SLOT_COUNT):
            if r[4 + 3 * i] not in KNOWN_BUTTON_TYPES:
                return True

        # A torn/transitional read (caught mid-EEPROM-commit) doesn't
        # necessarily come back fully zeroed in every slot — in
        # practice it showed up as MOST slots zeroed with one or two
        # slots holding a technically-valid-looking but implausible
        # partial value (e.g. type=32/mouse-button with code1=0, which
        # isn't a real button bitmask). A real configured mouse should
        # essentially never have a majority of its 6 slots reading as
        # entirely unassigned (type=0) at once, so treat that as a
        # strong signal something's still settling.
        empty_slots = sum(
            1 for i in range(BUTTON_SLOT_COUNT)
            if r[4 + 3 * i] == 0 and r[5 + 3 * i] == 0 and r[6 + 3 * i] == 0
        )
        if empty_slots >= 3:
            return True

        return False

    resp = send_and_read(
        dev, arr, min_resp_len=4 + BUTTON_SLOT_COUNT * 3,
        retry_on=looks_wrong, attempts=6,
    )
    bindings = []
    for i in range(BUTTON_SLOT_COUNT):
        offset = 4 + 3 * i
        bindings.append((resp[offset], resp[offset + 1], resp[offset + 2]))
    return bindings


def set_button_bindings(dev, bindings):
    """
    Writes all 6 button slots at once (same whole-table pattern as DPI
    — the protocol has no single-slot write). `bindings` is a list of
    up to 6 (type, code1, code2) tuples; any slots beyond len(bindings)
    keep their factory-default binding from DEFAULT_BUTTON_BINDINGS
    (matching exactly what the driver's own setMouseKeys() does — it
    always starts from the same hardcoded defaults before overwriting).
    """
    if len(bindings) > BUTTON_SLOT_COUNT:
        raise ValueError(f"at most {BUTTON_SLOT_COUNT} bindings, got {len(bindings)}")

    arr = [0x00] * 33
    arr[1] = 9  # cmd 0x09 = setMouseKeys
    arr[3] = 1
    arr[4] = 15

    offset = 5
    for i in range(BUTTON_SLOT_COUNT):
        type_, code1, code2 = bindings[i] if i < len(bindings) else DEFAULT_BUTTON_BINDINGS[i]
        arr[offset:offset + 3] = [type_ & 0xFF, code1 & 0xFF, code2 & 0xFF]
        offset += 3

    # Button-table commits appear to need noticeably longer to settle
    # than DPI/rate writes — 0.3s and then 0.5s both still produced a
    # transitional/mostly-zero read immediately afterward during
    # testing. 1.0s is a generous buffer; it only costs real time on
    # an explicit user-initiated write, not on every read.
    resp = send_write_command(dev, arr, settle_delay=1.0)
    if resp is None:
        print("[warn] no ack received for set_button_bindings — verify with a read-back before trusting the change", file=sys.stderr)
    return resp


def set_single_button(dev, slot, type_, code1, code2=0):
    """slot is 1-6. Reads the current bindings, changes just one slot, writes the whole table back."""
    if not (1 <= slot <= BUTTON_SLOT_COUNT):
        raise ValueError(f"slot must be 1-{BUTTON_SLOT_COUNT}")
    bindings = get_button_bindings(dev)
    bindings[slot - 1] = (type_, code1, code2)
    set_button_bindings(dev, bindings)


def reset_button_bindings(dev):
    """Restores all 6 button slots to factory defaults."""
    set_button_bindings(dev, [])
