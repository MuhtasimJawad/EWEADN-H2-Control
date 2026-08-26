#!/usr/bin/env python3
"""
Control an EWEADN H2 mouse over HID: read battery, read/set DPI levels,
read/set USB polling (report) rate, and remap the 6 buttons (standard
mouse buttons, DPI-cycle, fire-key, media keys, and keyboard shortcuts).

All protocol details (HID plumbing, command bytes, DPI encode/decode,
button-binding tables) live in h2_protocol.py — this file is just the
CLI wrapper around it, plus the --daemon status-bar integration. See
h2_protocol.py's module docstring for the reverse-engineering notes.

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

    python3 h2_battery.py --get-buttons           # show current button bindings
    python3 h2_battery.py --set-button 4 2 0      # rebind slot 4 to act as right-click
    python3 h2_battery.py --set-dpi-cycle-button 6 loop     # slot 6 -> DPI Loop+
    python3 h2_battery.py --set-fire-button 3 fire            # slot 3 -> rapid-fire click
    python3 h2_battery.py --set-media-button 4 play_pause      # slot 4 -> media play/pause
    python3 h2_battery.py --set-keyboard-button 2 ctrl+shift c # slot 2 -> Ctrl+Shift+C
    python3 h2_battery.py --reset-buttons         # restore factory button defaults
"""

import argparse
import json
import os
import sys
import time

from h2_protocol import (
    open_device, get_battery_info, get_report_rate, set_report_rate,
    get_dpi_config, set_single_dpi, set_active_dpi, set_dpi_count,
    get_button_bindings, set_single_button, reset_button_bindings,
    MOUSE_BUTTON_TYPE, DPI_CYCLE_TYPE, DPI_CYCLE_CODES,
    FIRE_KEY_TYPE, FIRE_KEY_CODES, MEDIA_KEY_TYPE, MEDIA_KEY_CODES,
    KEYBOARD_SHORTCUT_TYPE, MODIFIER_BITS, parse_modifiers, parse_key_name,
    hid_to_key_name,
)

# --daemon mode settings
STATE_FILE = os.path.expanduser("~/.local/state/h2-battery.json")
POLL_INTERVAL_SECONDS = 60


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
    parser.add_argument("--set-dpi-count", type=int, metavar="COUNT",
                         help="set how many DPI slots (1-6) are in rotation on the onboard "
                              "DPI button, without changing values or the active slot. "
                              "Mainly a recovery tool if dpi_count ever ends up wrong.")
    parser.add_argument("--get-buttons", action="store_true", help="show current button bindings for all 6 slots")
    parser.add_argument("--set-button", nargs=3, type=int, metavar=("SLOT", "MOUSE_BUTTON", "UNUSED"),
                         help="rebind button SLOT (1-6) to act as mouse button MOUSE_BUTTON "
                              "(1=left, 2=right, 4=middle, 8=back, 16=forward). Third arg is "
                              "unused for this binding type but required — pass 0.")
    parser.add_argument("--set-dpi-cycle-button", nargs=2, metavar=("SLOT", "MODE"),
                         help=f"rebind SLOT (1-6) to a DPI-cycle action. MODE is one of: {list(DPI_CYCLE_CODES)}")
    parser.add_argument("--set-fire-button", nargs=2, metavar=("SLOT", "MODE"),
                         help=f"rebind SLOT (1-6) to a fire-key action. MODE is one of: {list(FIRE_KEY_CODES)}")
    parser.add_argument("--set-media-button", nargs=2, metavar=("SLOT", "KEY"),
                         help=f"rebind SLOT (1-6) to a media key. KEY is one of: {list(MEDIA_KEY_CODES)}")
    parser.add_argument("--set-keyboard-button", nargs=3, metavar=("SLOT", "MODIFIERS", "KEY"),
                         help="rebind SLOT (1-6) to a keyboard shortcut. MODIFIERS is a "
                              "'+'-joined combo of ctrl/shift/alt/win (or 'none'), KEY is a "
                              "key name like c, enter, f5, space, left (see KEY_NAME_TO_HID "
                              "in h2_protocol.py for the full list).")
    parser.add_argument("--reset-buttons", action="store_true", help="restore all 6 button slots to factory defaults")
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

        if args.set_dpi_count is not None:
            info = fresh(get_battery_info)
            fresh(set_dpi_count, args.set_dpi_count, info["ic_type"])
            print(f"DPI count set to {args.set_dpi_count}")
            print(fresh(get_dpi_config, info["ic_type"]))
            return

        dpi_cycle_names = {v: k for k, v in DPI_CYCLE_CODES.items()}
        fire_key_names = {v: k for k, v in FIRE_KEY_CODES.items()}
        media_key_names = {v: k for k, v in MEDIA_KEY_CODES.items()}
        modifier_names = {v: k for k, v in MODIFIER_BITS.items()}

        def describe_binding(type_, code1, code2):
            if type_ == MOUSE_BUTTON_TYPE:
                names = {1: "left click", 2: "right click", 4: "middle click",
                          8: "back", 16: "forward"}
                return names.get(code1, f"mouse button (code1={code1})")
            elif type_ == FIRE_KEY_TYPE:
                return fire_key_names.get(code2, f"fire-key group (raw: code1={code1}, code2={code2})")
            elif type_ == DPI_CYCLE_TYPE:
                return dpi_cycle_names.get(code1, f"DPI cycle (raw: code1={code1})")
            elif type_ == MEDIA_KEY_TYPE:
                return media_key_names.get((code1, code2), f"media key (raw: code1={code1}, code2={code2})")
            elif type_ == KEYBOARD_SHORTCUT_TYPE:
                mods = "+".join(modifier_names[b] for b in (1, 2, 4, 8) if code1 & b) or "none"
                return f"keyboard shortcut ({mods}+{hid_to_key_name(code2)})" if mods != "none" else f"keyboard shortcut ({hid_to_key_name(code2)})"
            elif type_ == 160:
                return f"macro (raw: code1={code1}, code2={code2})"
            else:
                return f"unknown type={type_} (raw: code1={code1}, code2={code2})"

        def print_bindings(bindings):
            for i, (type_, code1, code2) in enumerate(bindings, start=1):
                print(f"Slot {i}: {describe_binding(type_, code1, code2)}")

        if args.get_buttons:
            print_bindings(fresh(get_button_bindings))
            return

        if args.set_button is not None:
            slot, mouse_button, _unused = args.set_button
            fresh(set_single_button, slot, MOUSE_BUTTON_TYPE, mouse_button, 0)
            print(f"Button slot {slot} rebound to mouse button {mouse_button} — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
            return

        if args.set_dpi_cycle_button is not None:
            slot_str, mode = args.set_dpi_cycle_button
            slot = int(slot_str)
            if mode not in DPI_CYCLE_CODES:
                raise ValueError(f"Unknown mode '{mode}'. Valid: {list(DPI_CYCLE_CODES)}")
            fresh(set_single_button, slot, DPI_CYCLE_TYPE, DPI_CYCLE_CODES[mode], 0)
            print(f"Button slot {slot} rebound to DPI-cycle '{mode}' — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
            return

        if args.set_fire_button is not None:
            slot_str, mode = args.set_fire_button
            slot = int(slot_str)
            if mode not in FIRE_KEY_CODES:
                raise ValueError(f"Unknown mode '{mode}'. Valid: {list(FIRE_KEY_CODES)}")
            fresh(set_single_button, slot, FIRE_KEY_TYPE, 100, FIRE_KEY_CODES[mode])
            print(f"Button slot {slot} rebound to fire-key '{mode}' — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
            return

        if args.set_media_button is not None:
            slot_str, key = args.set_media_button
            slot = int(slot_str)
            if key not in MEDIA_KEY_CODES:
                raise ValueError(f"Unknown key '{key}'. Valid: {list(MEDIA_KEY_CODES)}")
            code1, code2 = MEDIA_KEY_CODES[key]
            fresh(set_single_button, slot, MEDIA_KEY_TYPE, code1, code2)
            print(f"Button slot {slot} rebound to media key '{key}' — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
            return

        if args.set_keyboard_button is not None:
            slot_str, modifiers, key_name = args.set_keyboard_button
            slot = int(slot_str)
            code1 = 0 if modifiers.lower() == "none" else parse_modifiers(modifiers)
            code2 = parse_key_name(key_name)
            fresh(set_single_button, slot, KEYBOARD_SHORTCUT_TYPE, code1, code2)
            shortcut_desc = f"{modifiers}+{key_name}" if modifiers.lower() != "none" else key_name
            print(f"Button slot {slot} rebound to keyboard shortcut ({shortcut_desc}) — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
            return

        if args.reset_buttons:
            fresh(reset_button_bindings)
            print("All button bindings reset to factory defaults — reading back to confirm:")
            print_bindings(fresh(get_button_bindings))
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
