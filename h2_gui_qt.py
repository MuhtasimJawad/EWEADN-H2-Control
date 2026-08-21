#!/usr/bin/env python3
"""
h2_gui_qt.py — Qt control panel for the EWEADN H2 mouse.

Shows model, firmware version, and battery percentage. Lets you change
the USB polling rate via a dropdown, and edit all 6 DPI slots via
linked spin boxes + sliders, including which slot is active and how
many slots are in rotation. Supports fullscreen (F11 to toggle, Esc to
exit fullscreen).

Requires: pip install PySide6 hidapi --break-system-packages
Requires a udev rule granting access to the H2's hidraw node (see the
99-eweadn-h2.rules note from the CLI script setup) so this runs without root.
"""

import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QKeySequence, QShortcut, QPalette, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QComboBox, QPushButton, QSpinBox, QSlider,
    QRadioButton, QButtonGroup, QMessageBox, QStatusBar, QCheckBox,
    QGraphicsOpacityEffect,
)

try:
    import hid
except ImportError:
    print("Missing dependency: pip install hidapi --break-system-packages", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# HID protocol — identical to h2_battery.py / h2_gui.py (see h2_battery.py
# for the full reverse-engineering notes on where each command/offset came
# from).
# ---------------------------------------------------------------------------

VENDOR_ID = 0x089D   # wireless (2.4G dongle) mode
PRODUCT_ID = 0x062F

# The mouse presents a DIFFERENT USB identity while connected via cable
# (also the mode it's in while charging) — the dongle's vendor-command
# channel appears to stop responding once wired charging is active,
# even though the dongle's HID interfaces are still enumerated.
WIRED_VENDOR_ID = 0x088D
WIRED_PRODUCT_ID = 0x062E

# Tried in order — wired first, since that's the identity active while
# charging. Falls through to the wireless dongle identity otherwise.
KNOWN_DEVICE_IDS = [
    (WIRED_VENDOR_ID, WIRED_PRODUCT_ID),
    (VENDOR_ID, PRODUCT_ID),
]

READ_TIMEOUT_MS = 1000
REPORT_RATE_TO_CODE = {125: 8, 250: 4, 500: 2, 1000: 1}
CODE_TO_REPORT_RATE = {v: k for k, v in REPORT_RATE_TO_CODE.items()}
DPI_SLOT_COUNT = 6
ASYNC_STATUS_MARKERS = (209, 210)  # unsolicited status pushes, not command replies


def find_interface_path():
    """
    Tries each known device identity in order (wired first, since
    that's the one active while charging). NOTE: the wired identity's
    interface layout (which interface number is the vendor-control
    channel) hasn't been independently confirmed via lsusb -v — if
    reads still fail while wired, that's the next thing to check.
    """
    for vendor_id, product_id in KNOWN_DEVICE_IDS:
        candidates = hid.enumerate(vendor_id, product_id)
        if not candidates:
            continue
        for c in candidates:
            if c.get("interface_number") == 2:
                return c["path"]
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
    dev.set_nonblocking(True)
    for _ in range(max_drain):
        if not dev.read(64):
            break
    dev.set_nonblocking(False)


def checksum(arr):
    return sum(arr[5:32]) & 0xFF


def send_and_read(dev, arr, min_resp_len=5, retry_on=None, attempts=3):
    """Safe to retry — used for READ-only commands."""
    arr = list(arr)
    arr[32] = checksum(arr)
    last_resp = None
    for attempt in range(attempts):
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
        last_resp = resp
        if resp and len(resp) >= min_resp_len:
            if retry_on is None or not retry_on(resp):
                return resp
        if attempt < attempts - 1:
            time.sleep(0.15 * (attempt + 1))
    raise RuntimeError(f"Unexpected/empty response after {attempts} attempts: {last_resp}")


def send_write_command(dev, arr, settle_delay=0.3):
    """
    Never retries automatically — resending a write while the device is
    still committing the previous one (EEPROM) can corrupt its stored
    config. See h2_battery.py notes for the incident that proved this.
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
    return resp


def encode_dpi(value, ic_type=17):
    if ic_type != 17:
        raise NotImplementedError(f"DPI encoding only implemented for ic_type 17 (got {ic_type})")
    if value >= 13000:
        return ((value - 13000) // 1000) + 221
    elif value > 10000:
        return ((value - 10000) // 100) + 200
    else:
        return value // 50


def decode_dpi(raw, ic_type=17):
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


def _read_device_info(dev):
    arr = [0x00, 0x30] + [0x00] * 31
    return send_and_read(dev, arr, min_resp_len=15, retry_on=lambda r: r[11] == 0 or r[10] == 0)


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
    arr = [0x00, 18] + [0x00] * 31
    resp = send_and_read(dev, arr, retry_on=lambda r: r[4] not in CODE_TO_REPORT_RATE)
    return CODE_TO_REPORT_RATE.get(resp[4], resp[4])


def set_report_rate(dev, hz):
    if hz not in REPORT_RATE_TO_CODE:
        raise ValueError(f"Unsupported rate {hz}Hz")
    arr = [0x00] * 33
    arr[1] = 2
    arr[3] = 1
    arr[4] = 1
    arr[5] = REPORT_RATE_TO_CODE[hz]
    return send_write_command(dev, arr)


def get_dpi_config(dev, ic_type=17):
    arr = [0x00, 19] + [0x00] * 31
    slot_offsets = [5, 9, 13, 17, 21, 25]

    def looks_empty(r):
        return all(r[o] == 0 and r[o + 1] == 0 for o in slot_offsets)

    resp = send_and_read(dev, arr, retry_on=looks_empty)
    dpi_index = (resp[4] >> 4) & 0x0F
    dpi_count = resp[4] & 0x0F
    levels = [decode_dpi(_combine(resp[o], resp[o + 1]), ic_type) for o in slot_offsets]
    return {"dpi_index": dpi_index, "dpi_count": dpi_count, "levels": levels}


def set_dpi_config(dev, levels, dpi_index, dpi_count, ic_type=17):
    if len(levels) != DPI_SLOT_COUNT:
        raise ValueError(f"levels must have exactly {DPI_SLOT_COUNT} entries")
    arr = [0x00] * 33
    arr[1] = 3
    arr[3] = 1
    arr[4] = 26
    arr[6] = (dpi_count & 0x0F) | ((dpi_index & 0x0F) << 4)
    offset = 7
    for value in levels:
        enc = encode_dpi(value, ic_type)
        lo, hi = _lo(enc), _hi(enc)
        arr[offset:offset + 4] = [lo, hi, lo, hi]
        offset += 4
    return send_write_command(dev, arr)


# ---------------------------------------------------------------------------
# Toast notification — small auto-dismissing overlay, used instead of a
# persistent status bar message for "applied" confirmations.
# ---------------------------------------------------------------------------

class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background-color: rgba(40, 40, 40, 230);"
            "color: white;"
            "border-radius: 10px;"
            "padding: 10px 18px;"
            "font-size: 13px;"
        )
        self.hide()

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(1.0)

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._start_fade_out)

        self._fade_anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_anim.setDuration(400)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.finished.connect(self.hide)

    def show_message(self, text, duration_ms=2200):
        self._fade_anim.stop()
        self._effect.setOpacity(1.0)
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._show_timer.start(duration_ms)

    def reposition(self):
        parent_rect = self.parentWidget().rect()
        x = (parent_rect.width() - self.width()) // 2
        y = parent_rect.height() - self.height() - 40
        self.move(max(0, x), max(0, y))

    def _start_fade_out(self):
        self._fade_anim.start()


def build_dark_palette():
    """A standard dark palette; paired with the Fusion style (set in
    main()), which is the one Qt style guaranteed to honor a custom
    QPalette fully across platforms/desktops."""
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(37, 37, 38))
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ToolTipBase, Qt.white)
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(66, 133, 244))
    palette.setColor(QPalette.Highlight, QColor(66, 133, 244))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(127, 127, 127))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(127, 127, 127))
    return palette


# ---------------------------------------------------------------------------
# Background worker — runs HID calls off the UI thread
# ---------------------------------------------------------------------------

class Worker(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001 — surfaced to the UI, not swallowed
            self.error.emit(str(e))
            return
        self.done.emit(result)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class H2ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EWEADN H2 Control")
        self.resize(480, 640)

        self.ic_type = 17
        self._workers = []  # keep references so QThreads aren't GC'd mid-run
        self._light_palette = QApplication.instance().palette()

        self._build_ui()
        self._build_shortcuts()

        self.refresh()
        self._battery_timer = QTimer(self)
        self._battery_timer.timeout.connect(self.refresh_battery)
        self._battery_timer.start(60_000)

    # ---- UI construction ----

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top_bar.addWidget(refresh_btn)
        top_bar.addStretch()
        self.dark_mode_checkbox = QCheckBox("Dark Mode")
        self.dark_mode_checkbox.toggled.connect(self.toggle_dark_mode)
        top_bar.addWidget(self.dark_mode_checkbox)
        fullscreen_btn = QPushButton("Fullscreen (F11)")
        fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        top_bar.addWidget(fullscreen_btn)
        layout.addLayout(top_bar)

        # --- Device info ---
        device_box = QGroupBox("Device")
        device_grid = QGridLayout(device_box)
        self.model_label = QLabel("—")
        self.fw_label = QLabel("—")
        self.battery_label = QLabel("—")
        device_grid.addWidget(QLabel("Model:"), 0, 0)
        device_grid.addWidget(self.model_label, 0, 1)
        device_grid.addWidget(QLabel("Firmware:"), 1, 0)
        device_grid.addWidget(self.fw_label, 1, 1)
        device_grid.addWidget(QLabel("Battery:"), 2, 0)
        device_grid.addWidget(self.battery_label, 2, 1)
        layout.addWidget(device_box)

        # --- Polling rate ---
        rate_box = QGroupBox("Polling Rate")
        rate_row = QHBoxLayout(rate_box)
        self.rate_combo = QComboBox()
        self.rate_combo.addItems(["125", "250", "500", "1000"])
        self.rate_combo.setCurrentText("500")
        rate_row.addWidget(self.rate_combo)
        rate_row.addWidget(QLabel("Hz"))
        rate_apply_btn = QPushButton("Apply")
        rate_apply_btn.clicked.connect(self.apply_rate)
        rate_row.addWidget(rate_apply_btn)
        rate_row.addStretch()
        layout.addWidget(rate_box)

        # --- DPI ---
        dpi_box = QGroupBox("DPI Levels")
        dpi_layout = QVBoxLayout(dpi_box)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Active levels (count):"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, DPI_SLOT_COUNT)
        self.count_spin.setValue(DPI_SLOT_COUNT)
        count_row.addWidget(self.count_spin)
        count_row.addStretch()
        dpi_layout.addLayout(count_row)

        dpi_grid = QGridLayout()
        self.active_group = QButtonGroup(self)
        self.slot_spins = []
        for i in range(DPI_SLOT_COUNT):
            radio = QRadioButton(f"Slot {i + 1}")
            self.active_group.addButton(radio, i)
            if i == 0:
                radio.setChecked(True)

            spin = QSpinBox()
            spin.setRange(50, 32000)
            spin.setSingleStep(50)
            spin.setValue(800)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(50, 32000)
            slider.setSingleStep(50)
            slider.setPageStep(500)
            slider.setValue(800)

            # Bidirectional sync — Qt only emits valueChanged when the
            # value actually differs, so no manual re-entrancy guard needed.
            spin.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(spin.setValue)

            dpi_grid.addWidget(radio, i, 0)
            dpi_grid.addWidget(spin, i, 1)
            dpi_grid.addWidget(slider, i, 2)

            self.slot_spins.append(spin)

        dpi_layout.addLayout(dpi_grid)

        apply_dpi_btn = QPushButton("Apply DPI Settings")
        apply_dpi_btn.clicked.connect(self.apply_dpi)
        dpi_layout.addWidget(apply_dpi_btn)

        layout.addWidget(dpi_box)
        layout.addStretch()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Starting…")

        self.toast = Toast(central)

    def _build_shortcuts(self):
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, activated=self.exit_fullscreen)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()

    def toggle_dark_mode(self, checked):
        app = QApplication.instance()
        app.setPalette(build_dark_palette() if checked else self._light_palette)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.reposition()

    # ---- background task helper ----

    def _run_bg(self, fn, on_done=None):
        worker = Worker(fn)
        self._workers.append(worker)

        def cleanup():
            if worker in self._workers:
                self._workers.remove(worker)

        if on_done:
            worker.done.connect(on_done)
        worker.error.connect(self._show_error)
        worker.finished.connect(cleanup)
        worker.start()

    def _show_error(self, msg):
        self.status_bar.showMessage(f"Error: {msg}")
        QMessageBox.critical(self, "H2 Control", msg)

    # ---- device actions ----

    def refresh_battery(self):
        def task():
            dev = open_device()
            try:
                return get_battery_info(dev)
            finally:
                dev.close()

        def done(info):
            charging = " (charging)" if info["charge_flag"] else ""
            self.battery_label.setText(f"{info['battery_value']}%{charging}")
            self.model_label.setText(f"EWEADN H2 ({info['device_id']})")
            self.fw_label.setText(info["firmware_version"])
            self.ic_type = info["ic_type"]

        self._run_bg(task, done)

    def refresh(self):
        self.status_bar.showMessage("Reading current settings…")

        def task():
            dev = open_device()
            try:
                info = get_battery_info(dev)
                time.sleep(0.1)
                rate = get_report_rate(dev)
                time.sleep(0.1)
                dpi = get_dpi_config(dev, ic_type=info["ic_type"])
                return info, rate, dpi
            finally:
                dev.close()

        def done(result):
            info, rate, dpi = result
            charging = " (charging)" if info["charge_flag"] else ""
            self.battery_label.setText(f"{info['battery_value']}%{charging}")
            self.model_label.setText(f"EWEADN H2 ({info['device_id']})")
            self.fw_label.setText(info["firmware_version"])
            self.ic_type = info["ic_type"]

            self.rate_combo.setCurrentText(str(rate))
            self.count_spin.setValue(dpi["dpi_count"] or 1)
            active_btn = self.active_group.button(dpi["dpi_index"])
            if active_btn:
                active_btn.setChecked(True)
            for spin, val in zip(self.slot_spins, dpi["levels"]):
                spin.setValue(val)

            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)

    def apply_rate(self):
        hz = int(self.rate_combo.currentText())
        self.status_bar.showMessage(f"Setting rate to {hz}Hz…")

        def task():
            dev = open_device()
            try:
                set_report_rate(dev, hz)
            finally:
                dev.close()

        def done(_):
            self.toast.show_message(f"Polling rate set to {hz}Hz")
            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)

    def apply_dpi(self):
        levels = [spin.value() for spin in self.slot_spins]
        active = self.active_group.checkedId()
        if active < 0:
            QMessageBox.warning(self, "H2 Control", "Select an active DPI slot first.")
            return
        count = self.count_spin.value()
        ic_type = self.ic_type
        self.status_bar.showMessage("Writing DPI settings…")

        def task():
            dev = open_device()
            try:
                set_dpi_config(dev, levels, active, count, ic_type)
            finally:
                dev.close()

        def done(_):
            self.toast.show_message("DPI settings applied")
            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("h2-control")
    app.setStyle("Fusion")  # the one style that reliably honors a custom QPalette
    window = H2ControlWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
