#!/usr/bin/env python3
"""
Control an EWEADN H2 mouse over HID: read battery, read/set DPI levels,
read/set USB polling (report) rate.

Protocol reverse-engineered from the official web driver
(hub.eweadn.cn/pq/h2) — specifically getDeviceInfo(), getDpiData(),
setDpiValue(), getReportRate(), and setReportRate() in the driver's JS,
plus the v()/k() DPI value encode/decode helpers and the setData()
checksum wrapper shared by every command.

Requires: pip install hidapi --break-system-packages
Requires read/write permission on the device's hidraw node — add a
udev rule (see README notes) so this doesn't need sudo.

Usage:
    python3 h2_battery.py                      # print battery info once
    python3 h2_battery.py --json                # battery info as JSON (for status bar modules)
    python3 h2_battery.py --daemon               # poll battery forever, write state file

    python3 h2_battery.py --get-config           # show battery + DPI levels + polling rate
    python3 h2_battery.py --set-rate 1000         # set polling rate (125/250/500/1000 Hz)
    python3 h2_battery.py --set-dpi 3 3200        # set DPI slot 3 (1-6) to 3200
    python3 h2_battery.py --set-active-dpi 2      # switch the active DPI slot to 2 (1-6)
"""

import argparse
import json
import os
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

# --daemon mode settings
STATE_FILE = os.path.expanduser("~/.local/state/h2-battery.json")
POLL_INTERVAL_SECONDS = 60

# USB/dongle polling rate <-> device register code, taken directly from
# the driver's "usbReportRate"/"dongleReportRate" option lists.
REPORT_RATE_TO_CODE = {125: 8, 250: 4, 500: 2, 1000: 1}
CODE_TO_REPORT_RATE = {v: k for k, v in REPORT_RATE_TO_CODE.items()}

DPI_SLOT_COUNT = 6


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


ASYNC_STATUS_MARKERS = (209, 210)  # unsolicited status pushes, not command replies


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
# Commands
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
        print("[warn] no ack received for set_report_rate — verify with --get-config before trusting the change", file=sys.stderr)


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
        print("[warn] no ack received for set_dpi_config — verify with --get-config before trusting the change", file=sys.stderr)


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


# ---------------------------------------------------------------------------
# Bar/daemon helpers
# ---------------------------------------------------------------------------

def to_bar_dict(info):
    charging = " (charging)" if info["charge_flag"] else ""
    return {
        "percent": info["battery_value"],
        "text": f"{info['battery_value']}%",
        "tooltip": f"EWEADN H2{charging} — fw {info['firmware_version']}",
        "class": "charging" if info["charge_flag"] else "discharging",
        "updated": int(time.time()),
    }


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


def run_daemon():
    """
    Poll the mouse forever and write the latest battery reading to
    STATE_FILE. Meant to run under systemd --user as a long-lived
    service; caelestia (or any status bar) just reads/watches
    STATE_FILE and never has to touch the hidraw device directly.
    """
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"h2-battery daemon started, writing to {STATE_FILE} every {POLL_INTERVAL_SECONDS}s", file=sys.stderr)

    while True:
        try:
            dev = open_device()
            try:
                info = get_battery_info(dev)
            finally:
                dev.close()
            payload = to_bar_dict(info)
        except Exception as e:
            print(f"[warn] read failed: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, STATE_FILE)  # atomic write, avoids readers seeing a half-written file

        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EWEADN H2 mouse control (battery, DPI, polling rate)")
    parser.add_argument("--json", action="store_true", help="print battery info as JSON")
    parser.add_argument("--daemon", action="store_true", help="poll battery forever, writing STATE_FILE")
    parser.add_argument("--get-config", action="store_true", help="show battery + DPI levels + polling rate")
    parser.add_argument("--set-rate", type=int, metavar="HZ", help="set polling rate: 125, 250, 500, or 1000")
    parser.add_argument("--set-dpi", nargs=2, type=int, metavar=("SLOT", "VALUE"),
                         help="set DPI slot (1-6) to VALUE, e.g. --set-dpi 3 3200")
    parser.add_argument("--set-active-dpi", type=int, metavar="SLOT",
                         help="switch the active DPI slot (1-6) without changing values")
    args = parser.parse_args()

    if args.daemon:
        try:
            run_daemon()
        except KeyboardInterrupt:
            sys.exit(0)
        return

    def fresh(fn, *fn_args):
        """
        Opens a brand-new connection for a single command and closes it
        immediately after. Deliberately NOT reusing one connection across
        multiple chained commands (e.g. battery -> rate -> dpi) — a
        leftover/delayed reply from an earlier command was observed
        bleeding into the next command's read on a shared connection,
        producing corrupted data. A fresh handle per command avoids that.
        """
        dev = open_device()
        try:
            return fn(dev, *fn_args)
        finally:
            dev.close()

    try:
        if args.set_rate is not None:
            fresh(set_report_rate, args.set_rate)
            print(f"Polling rate set to {args.set_rate}Hz")
            return

        if args.set_dpi is not None:
            slot, value = args.set_dpi
            info = fresh(get_battery_info)  # need ic_type for correct encoding
            fresh(set_single_dpi, slot, value, info["ic_type"])
            print(f"DPI slot {slot} set to {value}")
            return

        if args.set_active_dpi is not None:
            info = fresh(get_battery_info)
            fresh(set_active_dpi, args.set_active_dpi, info["ic_type"])
            print(f"Active DPI slot switched to {args.set_active_dpi}")
            return

        if args.get_config:
            info = fresh(get_battery_info)
            rate = fresh(get_report_rate)
            dpi = fresh(get_dpi_config, info["ic_type"])
            print(f"Battery:      {info['battery_value']}%"
                  + (" (charging)" if info["charge_flag"] else ""))
            print(f"Firmware:     {info['firmware_version']}")
            print(f"Polling rate: {rate}Hz")
            print(f"DPI levels:   {dpi['levels']}  (active slot: {dpi['dpi_index'] + 1}, count: {dpi['dpi_count']})")
            return

        # Default: just print battery info (single-shot / --json)
        try:
            info = fresh(get_battery_info)
        except Exception as e:
            if args.json:
                print(json.dumps({"text": "N/A", "tooltip": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(json.dumps(to_bar_dict(info)))
        else:
            print(info)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
