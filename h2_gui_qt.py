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

# All HID/protocol logic (device discovery, command bytes, DPI/button
# encode-decode) lives in h2_protocol.py, shared with h2_battery.py — see
# that module's docstring for the reverse-engineering notes. This file
# used to duplicate all of it; importing instead means a protocol fix
# only has to be made in one place.
from h2_protocol import (
    DPI_SLOT_COUNT,
    open_device, get_battery_info, get_report_rate, set_report_rate,
    get_dpi_config, set_dpi_config,
)


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
            self.count_spin.setValue(dpi["dpi_count"])
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
