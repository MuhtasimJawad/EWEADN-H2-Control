#!/usr/bin/env python3
"""
h2_gui_qt.py — Qt control panel for the EWEADN H2 mouse.

Shows model, firmware version, and battery percentage. Lets you change
the USB polling rate via a dropdown, edit all 6 DPI slots via linked
spin boxes + sliders (including which slot is active and how many
slots are in rotation), and remap all 6 buttons — standard mouse
button, DPI cycle, fire key, media key, or a keyboard shortcut captured
by pressing it directly. Supports fullscreen (F11 to toggle, Esc to
exit fullscreen).

Requires: pip install PySide6 hidapi --break-system-packages
Requires a udev rule granting access to the H2's hidraw node (see the
99-eweadn-h2.rules note from the CLI script setup) so this runs without root.
"""

import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPropertyAnimation, QEasingCurve, QRectF
from PySide6.QtGui import QKeySequence, QShortcut, QPalette, QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QComboBox, QPushButton, QSpinBox, QSlider,
    QRadioButton, QButtonGroup, QMessageBox, QStatusBar, QCheckBox,
    QGraphicsOpacityEffect, QTabWidget, QStackedWidget,
)

# All HID/protocol logic (device discovery, command bytes, DPI/button
# encode-decode) lives in h2_protocol.py, shared with h2_battery.py — see
# that module's docstring for the reverse-engineering notes. This file
# used to duplicate all of it; importing instead means a protocol fix
# only has to be made in one place.
from h2_protocol import (
    DPI_SLOT_COUNT, BUTTON_SLOT_COUNT,
    MOUSE_BUTTON_TYPE, MOUSE_BUTTON_LEFT, MOUSE_BUTTON_RIGHT,
    MOUSE_BUTTON_MIDDLE, MOUSE_BUTTON_BACK, MOUSE_BUTTON_FORWARD,
    DPI_CYCLE_TYPE, DPI_CYCLE_CODES,
    FIRE_KEY_TYPE, FIRE_KEY_CODES,
    MEDIA_KEY_TYPE, MEDIA_KEY_CODES,
    KEYBOARD_SHORTCUT_TYPE, MODIFIER_BITS, KEY_NAME_TO_HID, hid_to_key_name,
    run_isolated, get_battery_info, get_report_rate, set_report_rate,
    get_dpi_config, set_dpi_config,
    get_button_bindings, set_button_bindings, reset_button_bindings,
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
# Theme-aware text colors — shared by every custom-stylesheet label in
# this file. A widget with ANY explicit stylesheet property set doesn't
# reliably keep inheriting the app QPalette's text color for whatever
# it left unspecified (this is what caused text to stay black-on-dark
# in an earlier build) — so any label that needs bold/italic/font-size
# styling gets its color set explicitly from these two constants, and
# re-set whenever dark mode toggles, rather than left to chance.
# ---------------------------------------------------------------------------

LIGHT_TEXT_COLOR = "#202020"
DARK_TEXT_COLOR = "#f0f0f0"


def _fill_color_for_percent(percent):
    """
    Pure/testable: which color a battery-level fill should be at a
    given percentage. Kept separate from BatteryIndicator's paintEvent
    so the actual threshold logic can be unit-tested without a real
    Qt display.
    """
    if percent is None:
        return "#808080"
    if percent <= 20:
        return "#e0392b"   # red
    if percent <= 40:
        return "#e0a92b"   # amber
    return "#3ea34d"       # green


class BatteryIndicator(QWidget):
    """
    Small custom-painted horizontal battery gauge with the live
    percentage drawn inside it — used instead of a plain "57%" text
    label so charge level is visible at a glance, not just readable.

    NOTE on charging accuracy: the mouse's own firmware occasionally
    reports a battery percentage that reads higher than the true level
    while actively charging (confirmed this isn't something our code
    is misreading — the official web driver decodes the exact same raw
    byte with no special-casing for the charging state either). This
    is common behavior for cheap voltage-based fuel-gauge ICs: the SOC
    estimate saturates near "full" once it hits the charging plateau
    voltage, before the battery is actually topped off. Nothing in
    software can correct a number the device itself is sending, so
    set_percent() attaches an explanatory tooltip whenever charging is
    reported, rather than silently presenting a possibly-inflated
    number as if it were precise.
    """

    _BODY_W = 74
    _BODY_H = 36
    _NUB_W = 5
    _NUB_H = 16
    _MARGIN = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._percent = None  # None until the first real reading
        self._charging = False
        self._dark_mode = False
        self.setFixedSize(self._BODY_W + self._NUB_W, self._BODY_H)

    def set_percent(self, percent, charging=False):
        self._percent = percent
        self._charging = charging
        if charging:
            self.setToolTip(
                "This percentage is reported directly by the mouse's firmware and "
                "can read higher than the true charge level while actively "
                "charging — this is a device behavior, not something this app "
                "can correct."
            )
        else:
            self.setToolTip("")
        self.update()

    def set_dark_mode(self, is_dark):
        self._dark_mode = is_dark
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        outline_color = QColor("#e8e8e8") if self._dark_mode else QColor("#303030")

        # body outline
        pen = QPen(outline_color)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        body_rect = QRectF(1, 1, self._BODY_W - 2, self._BODY_H - 2)
        painter.drawRoundedRect(body_rect, 4, 4)

        # terminal nub, on the right — standard horizontal-battery convention
        nub_y = (self._BODY_H - self._NUB_H) / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(outline_color))
        painter.drawRoundedRect(QRectF(self._BODY_W - 1, nub_y, self._NUB_W, self._NUB_H), 1, 1)

        # fill, proportional to percent, growing left-to-right, colored by level
        if self._percent is not None:
            pct = max(0, min(100, self._percent))
            fill_color = QColor(_fill_color_for_percent(pct))
            inner = body_rect.adjusted(self._MARGIN, self._MARGIN, -self._MARGIN, -self._MARGIN)
            fill_w = inner.width() * pct / 100.0
            fill_rect = QRectF(inner.x(), inner.y(), fill_w, inner.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawRoundedRect(fill_rect, 2, 2)

        # percentage text, drawn with a black outline behind white fill
        # so it stays legible over any fill color underneath (red,
        # amber, green) or the empty gray body — deliberately NOT tied
        # to the light/dark text-color constants above, since this
        # text sits on a colored fill rather than the app background.
        label = "—" if self._percent is None else f"{self._percent}%{'⚡' if self._charging else ''}"
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        text_rect = body_rect
        for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
            painter.setPen(QColor(0, 0, 0, 220))
            painter.drawText(text_rect.translated(dx, dy), Qt.AlignCenter, label)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(text_rect, Qt.AlignCenter, label)


# ---------------------------------------------------------------------------
# Qt key -> friendly key name, for the keyboard-shortcut capture widget.
#
# Qt.Key_A..Key_Z and Qt.Key_0..Key_9 conveniently share their values
# with plain ASCII ('A'-'Z', '0'-'9'), so those are derived rather than
# hardcoded. Everything else (Enter, F-keys, navigation, punctuation)
# is mapped explicitly to the exact same name strings h2_protocol's
# KEY_NAME_TO_HID already uses, so the GUI and CLI describe a shortcut
# identically.
# ---------------------------------------------------------------------------

def _build_qt_key_to_name():
    mapping = {}
    for code in range(ord('A'), ord('Z') + 1):
        mapping[code] = chr(code).lower()
    for code in range(ord('0'), ord('9') + 1):
        mapping[code] = chr(code)
    for n in range(1, 25):
        attr = f"Key_F{n}"
        if hasattr(Qt, attr):
            mapping[getattr(Qt, attr)] = f"f{n}"
    named = {
        "Key_Return": "enter", "Key_Enter": "enter", "Key_Escape": "esc",
        "Key_Backspace": "backspace", "Key_Tab": "tab", "Key_Space": "space",
        "Key_Minus": "minus", "Key_Equal": "equal",
        "Key_BracketLeft": "leftbracket", "Key_BracketRight": "rightbracket",
        "Key_Backslash": "backslash", "Key_Semicolon": "semicolon",
        "Key_Apostrophe": "quote", "Key_QuoteLeft": "grave",
        "Key_Comma": "comma", "Key_Period": "period", "Key_Slash": "slash",
        "Key_CapsLock": "capslock", "Key_Print": "printscreen",
        "Key_ScrollLock": "scrolllock", "Key_Pause": "pause",
        "Key_Insert": "insert", "Key_Home": "home", "Key_PageUp": "pageup",
        "Key_Delete": "delete", "Key_End": "end", "Key_PageDown": "pagedown",
        "Key_Right": "right", "Key_Left": "left", "Key_Down": "down",
        "Key_Up": "up", "Key_NumLock": "numlock",
    }
    for attr, name in named.items():
        if hasattr(Qt, attr):
            mapping[getattr(Qt, attr)] = name
    return mapping


_QT_KEY_TO_NAME = _build_qt_key_to_name()

# Modifier keys pressed on their own don't form a shortcut — capture
# waits for a real key while these are held, instead of committing
# "just Ctrl" as if it were a complete binding.
_PURE_MODIFIER_KEYS = {
    getattr(Qt, attr) for attr in
    ("Key_Control", "Key_Shift", "Key_Alt", "Key_Meta", "Key_AltGr",
     "Key_Super_L", "Key_Super_R")
    if hasattr(Qt, attr)
}


class KeyCaptureWidget(QWidget):
    """
    Displays a keyboard-shortcut binding and lets the user set a new
    one by pressing it directly, rather than typing modifier names and
    a raw USB HID code by hand (that's what h2_battery.py's
    --set-keyboard-button flag is for; this widget is the GUI's
    press-to-assign equivalent).
    """

    IDLE_BTN_TEXT = "Set…"
    CAPTURING_BTN_TEXT = "Cancel"
    CAPTURING_STATUS_TEXT = "Press keys… (Esc cancels)"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._code1 = 0
        self._code2 = 0
        self._capturing = False
        self._dark_mode = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)

        self.display = QLabel("(not set)")
        self.display.setMinimumWidth(150)
        self._apply_display_style()
        row.addWidget(self.display)

        self.set_btn = QPushButton(self.IDLE_BTN_TEXT)
        self.set_btn.setFixedWidth(96)
        self.set_btn.clicked.connect(self._toggle_capture)
        row.addWidget(self.set_btn)
        outer.addLayout(row)

        # Capture-in-progress status lives on its own line rather than
        # inside the button text — CAPTURING_BTN_TEXT used to be shown
        # as the button's own label, which overflowed/got clipped at
        # the button's fixed width. The button now only ever shows
        # short, fixed-width-safe text ("Set…" / "Cancel").
        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        outer.addWidget(self.status_label)

        self.setFocusPolicy(Qt.StrongFocus)
        self._apply_status_style()

    def _apply_status_style(self):
        color = DARK_TEXT_COLOR if self._dark_mode else LIGHT_TEXT_COLOR
        self.status_label.setStyleSheet(f"font-style: italic; font-size: 13px; color: {color};")

    def _apply_display_style(self):
        color = DARK_TEXT_COLOR if self._dark_mode else LIGHT_TEXT_COLOR
        self.display.setStyleSheet(
            f"background-color: rgba(127,127,127,40); border-radius: 4px; "
            f"padding: 3px 8px; color: {color};"
        )

    def set_dark_mode(self, is_dark):
        self._dark_mode = is_dark
        self._apply_display_style()
        self._apply_status_style()

    def set_binding(self, code1, code2):
        self._code1, self._code2 = code1, code2
        self.display.setText(self._describe(code1, code2))

    def get_binding(self):
        return self._code1, self._code2

    @staticmethod
    def _describe(code1, code2):
        if code2 == 0:
            return "(not set)"
        mod_names = [name for bit, name in
                     ((1, "Ctrl"), (2, "Shift"), (4, "Alt"), (8, "Win"))
                     if code1 & bit]
        key_name = hid_to_key_name(code2)
        key_label = key_name.upper() if len(key_name) <= 1 else key_name.capitalize()
        return "+".join(mod_names + [key_label])

    def _toggle_capture(self):
        if self._capturing:
            self._stop_capture(restore=True)
        else:
            self._start_capture()

    def _start_capture(self):
        self._capturing = True
        self.set_btn.setText(self.CAPTURING_BTN_TEXT)
        self.status_label.setText(self.CAPTURING_STATUS_TEXT)
        self.status_label.setVisible(True)
        self.display.setText("…")
        self.setFocus()
        self.grabKeyboard()

    def _stop_capture(self, restore):
        self._capturing = False
        self.releaseKeyboard()
        self.set_btn.setText(self.IDLE_BTN_TEXT)
        self.status_label.setVisible(False)
        self.status_label.setText("")
        if restore:
            self.display.setText(self._describe(self._code1, self._code2))

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return

        key = event.key()

        if key == Qt.Key_Escape:
            self._stop_capture(restore=True)
            return

        if key in _PURE_MODIFIER_KEYS:
            return  # still building the combo — wait for a real key

        name = _QT_KEY_TO_NAME.get(key)
        if name is None or name not in KEY_NAME_TO_HID:
            self.display.setText("Key not supported — try another")
            return

        mods = event.modifiers()
        code1 = 0
        if mods & Qt.ControlModifier:
            code1 |= MODIFIER_BITS["ctrl"]
        if mods & Qt.ShiftModifier:
            code1 |= MODIFIER_BITS["shift"]
        if mods & Qt.AltModifier:
            code1 |= MODIFIER_BITS["alt"]
        if mods & Qt.MetaModifier:
            code1 |= MODIFIER_BITS["win"]

        self.set_binding(code1, KEY_NAME_TO_HID[name])
        self._stop_capture(restore=False)


# ---------------------------------------------------------------------------
# One row in the Buttons tab — a Type dropdown plus whichever control
# fits that type. Rows that read back a binding type this GUI doesn't
# offer (type 50, or a type-160 macro) are locked read-only rather than
# guessed at — Apply Bindings writes such a row's original bytes back
# unchanged, so an unrelated slot's write never risks corrupting a
# macro binding the user set up some other way (e.g. the official web
# driver). Editing those stays a CLI-only / web-driver-only task.
# ---------------------------------------------------------------------------

class ButtonSlotRow(QWidget):
    TYPE_LABELS = ["Disabled", "Mouse Button", "DPI Cycle", "Fire Key", "Media Key", "Keyboard Shortcut"]

    # Factory-default physical position of each slot (see
    # DEFAULT_BUTTON_BINDINGS in h2_protocol.py) — shown instead of a
    # bare "Slot N" label, since "Slot 4" doesn't tell anyone which
    # physical button that is. This is a label only; it doesn't change
    # what a slot is currently bound to; slot 6 has always defaulted to
    # a DPI-cycle action rather than a click, hence "DPI Button" rather
    # than a mouse-click name.
    SLOT_NAMES = ["Left Button", "Right Button", "Middle Button",
                  "Back Button", "Forward Button", "DPI Button"]

    def __init__(self, slot_number, parent=None):
        super().__init__(parent)
        self.slot_number = slot_number
        self._locked = False
        self._raw_binding = (0, 0, 0)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        name_label = QLabel(f"{self.SLOT_NAMES[slot_number - 1]}:")
        name_label.setMinimumWidth(110)
        layout.addWidget(name_label)

        self.type_combo = QComboBox()
        self.type_combo.addItems(self.TYPE_LABELS)
        self.type_combo.setFixedWidth(140)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        layout.addWidget(self.type_combo)

        self.stack = QStackedWidget()

        self.stack.addWidget(QLabel("—"))  # 0: Disabled

        self.mouse_combo = QComboBox()
        for label, code1 in [
            ("Left Click", MOUSE_BUTTON_LEFT), ("Right Click", MOUSE_BUTTON_RIGHT),
            ("Middle Click", MOUSE_BUTTON_MIDDLE), ("Back", MOUSE_BUTTON_BACK),
            ("Forward", MOUSE_BUTTON_FORWARD),
        ]:
            self.mouse_combo.addItem(label, code1)
        self.stack.addWidget(self.mouse_combo)  # 1: Mouse Button

        self.dpi_cycle_combo = QComboBox()
        for key, label in [("loop", "DPI Loop+"), ("up", "DPI+"), ("down", "DPI-")]:
            self.dpi_cycle_combo.addItem(label, key)
        self.stack.addWidget(self.dpi_cycle_combo)  # 2: DPI Cycle

        self.fire_combo = QComboBox()
        for key, label in [("doubleclick", "Double Click"), ("fire", "Fire Key (rapid-fire)")]:
            self.fire_combo.addItem(label, key)
        self.stack.addWidget(self.fire_combo)  # 3: Fire Key

        self.media_combo = QComboBox()
        for key in MEDIA_KEY_CODES:
            self.media_combo.addItem(key.replace("_", " ").title(), key)
        self.stack.addWidget(self.media_combo)  # 4: Media Key

        self.key_capture = KeyCaptureWidget()
        self.stack.addWidget(self.key_capture)  # 5: Keyboard Shortcut

        layout.addWidget(self.stack, stretch=1)

        self.locked_label = QLabel("")
        self._dark_mode = False
        self._apply_locked_style()
        layout.addWidget(self.locked_label)

    def _apply_locked_style(self):
        # Amber at a contrast-adjusted shade per theme — a single fixed
        # amber reads fine on one background and washes out on the
        # other, so this gets the same theme-propagation treatment as
        # every other custom-stylesheet label in this file.
        color = "#ffb84d" if self._dark_mode else "#a06000"
        self.locked_label.setStyleSheet(f"color: {color}; font-style: italic;")

    def set_dark_mode(self, is_dark):
        self._dark_mode = is_dark
        self._apply_locked_style()
        self.key_capture.set_dark_mode(is_dark)

    def _on_type_changed(self, index):
        self.stack.setCurrentIndex(index)

    def _set_locked(self, locked, raw_type=None):
        self._locked = locked
        self.type_combo.setEnabled(not locked)
        self.stack.setEnabled(not locked)
        self.locked_label.setText(f"unsupported type {raw_type} — edit via CLI" if locked else "")

    def set_from_binding(self, type_, code1, code2):
        self._raw_binding = (type_, code1, code2)

        if type_ == 0:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(0)
        elif type_ == MOUSE_BUTTON_TYPE:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(1)
            i = self.mouse_combo.findData(code1)
            self.mouse_combo.setCurrentIndex(i if i >= 0 else 0)
        elif type_ == DPI_CYCLE_TYPE:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(2)
            key = next((k for k, v in DPI_CYCLE_CODES.items() if v == code1), None)
            i = self.dpi_cycle_combo.findData(key) if key else -1
            self.dpi_cycle_combo.setCurrentIndex(i if i >= 0 else 0)
        elif type_ == FIRE_KEY_TYPE:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(3)
            key = next((k for k, v in FIRE_KEY_CODES.items() if v == code2), None)
            i = self.fire_combo.findData(key) if key else -1
            self.fire_combo.setCurrentIndex(i if i >= 0 else 0)
        elif type_ == MEDIA_KEY_TYPE:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(4)
            key = next((k for k, v in MEDIA_KEY_CODES.items() if v == (code1, code2)), None)
            i = self.media_combo.findData(key) if key else -1
            self.media_combo.setCurrentIndex(i if i >= 0 else 0)
        elif type_ == KEYBOARD_SHORTCUT_TYPE:
            self._set_locked(False)
            self.type_combo.setCurrentIndex(5)
            self.key_capture.set_binding(code1, code2)
        else:
            # type 50 (unclear/grouped with 32 in the driver's own UI)
            # or type 160 (macro, deliberately unimplemented) — leave
            # it alone rather than guess.
            self._set_locked(True, raw_type=type_)

        self.stack.setCurrentIndex(self.type_combo.currentIndex())

    def get_binding(self):
        if self._locked:
            return self._raw_binding

        idx = self.type_combo.currentIndex()
        if idx == 0:
            return (0, 0, 0)
        elif idx == 1:
            return (MOUSE_BUTTON_TYPE, self.mouse_combo.currentData(), 0)
        elif idx == 2:
            key = self.dpi_cycle_combo.currentData()
            return (DPI_CYCLE_TYPE, DPI_CYCLE_CODES[key], 0)
        elif idx == 3:
            key = self.fire_combo.currentData()
            return (FIRE_KEY_TYPE, 100, FIRE_KEY_CODES[key])
        elif idx == 4:
            key = self.media_combo.currentData()
            code1, code2 = MEDIA_KEY_CODES[key]
            return (MEDIA_KEY_TYPE, code1, code2)
        elif idx == 5:
            code1, code2 = self.key_capture.get_binding()
            if code2 == 0:  # nothing captured yet — don't send a bogus keycode-0 shortcut
                return (0, 0, 0)
            return (KEYBOARD_SHORTCUT_TYPE, code1, code2)
        return (0, 0, 0)


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
        # Without this, shrinking the window squeezes widgets (the DPI
        # value spinboxes especially) below what they need to render
        # their text — Qt doesn't elide cleanly at that point, it
        # overlaps glyphs, which is what produced the garbled
        # "24000 DPI"-in-a-too-narrow-box look. A hard floor here
        # prevents that everywhere, not just for one widget.
        self.setMinimumSize(460, 560)

        self.ic_type = 17
        self._workers = []  # keep references so QThreads aren't GC'd mid-run
        self._busy = False
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

        self._groupboxes = []

        top_bar = QHBoxLayout()

        # --- Left: device name, top-left ---
        self.model_label = QLabel("EWEADN H2 (—)")
        top_bar.addWidget(self.model_label, alignment=Qt.AlignTop | Qt.AlignLeft)

        top_bar.addStretch()

        # --- Right: battery gauge with firmware version directly
        # beneath it — matches the reference layout's header (model
        # top-left, battery+firmware grouped top-right), still with
        # plain widget styling, no custom theme. ---
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(2)
        self.battery_indicator = BatteryIndicator()
        right_col.addWidget(self.battery_indicator, alignment=Qt.AlignRight)
        self.fw_label = QLabel("Firmware: —")
        self.fw_label.setAlignment(Qt.AlignRight)
        right_col.addWidget(self.fw_label, alignment=Qt.AlignRight)
        top_bar.addLayout(right_col)
        top_bar.setAlignment(right_col, Qt.AlignTop)

        layout.addLayout(top_bar)

        self._themed_labels = [self.model_label, self.fw_label]
        self._apply_themed_labels(False)

        # --- Slim utility row: Refresh / Fullscreen / Dark Mode.
        # These aren't part of the reference layout, so they get a
        # compact row of their own rather than being wedged into the
        # header. ---
        utility_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        utility_row.addWidget(self.refresh_btn)
        fullscreen_btn = QPushButton("Fullscreen (F11)")
        fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        utility_row.addWidget(fullscreen_btn)
        utility_row.addStretch()
        self.dark_mode_checkbox = QCheckBox("Dark Mode")
        self.dark_mode_checkbox.toggled.connect(self.toggle_dark_mode)
        utility_row.addWidget(self.dark_mode_checkbox)
        layout.addLayout(utility_row)

        tabs = QTabWidget()
        layout.addWidget(tabs, stretch=1)
        tabs.addTab(self._build_device_tab(), "Device")
        tabs.addTab(self._build_buttons_tab(), "Buttons")

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Starting…")

        self.toast = Toast(central)

    def _apply_themed_labels(self, is_dark):
        color = DARK_TEXT_COLOR if is_dark else LIGHT_TEXT_COLOR
        for label in self._themed_labels:
            label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _style_groupbox(self, box, is_dark):
        # Only restyles the title text (bold + theme color) — leaving
        # the rest of QGroupBox's stylesheet untouched keeps the
        # native frame/border intact rather than risking breaking it
        # with a broader override.
        color = DARK_TEXT_COLOR if is_dark else LIGHT_TEXT_COLOR
        box.setStyleSheet(
            f"QGroupBox::title {{ font-weight: bold; color: {color}; "
            f"subcontrol-origin: margin; left: 8px; padding: 0 3px; }}"
        )

    def _make_groupbox(self, title):
        box = QGroupBox(title)
        self._style_groupbox(box, False)
        self._groupboxes.append(box)
        return box

    def _build_device_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # --- Polling rate: segmented row of checkable buttons instead
        # of a dropdown, per the reference layout — still plain
        # QPushButtons with default styling, just checkable+exclusive
        # rather than a combo box. ---
        rate_box = self._make_groupbox("Polling Rate")
        rate_row = QHBoxLayout(rate_box)
        self.rate_buttons = {}
        rate_group = QButtonGroup(self)
        rate_group.setExclusive(True)
        for hz in (125, 250, 500, 1000):
            btn = QPushButton(f"{hz}Hz")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, h=hz: self._select_rate(h))
            rate_group.addButton(btn)
            rate_row.addWidget(btn)
            self.rate_buttons[hz] = btn
        self._select_rate(500)
        self.rate_apply_btn = QPushButton("Apply")
        self.rate_apply_btn.clicked.connect(self.apply_rate)
        rate_row.addWidget(self.rate_apply_btn)
        layout.addWidget(rate_box)

        # --- DPI: each slot is a "Slot N: XXXX DPI" label + value
        # spinbox on one line, with the slider on its own line below —
        # matches the reference layout's per-slot structure, still
        # with default widget styling. ---
        dpi_box = self._make_groupbox("DPI Levels")
        dpi_layout = QVBoxLayout(dpi_box)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Active levels (count):"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, DPI_SLOT_COUNT)
        self.count_spin.setValue(DPI_SLOT_COUNT)
        count_row.addWidget(self.count_spin)
        count_row.addStretch()
        dpi_layout.addLayout(count_row)

        self.active_group = QButtonGroup(self)
        self.slot_spins = []
        self.slot_name_labels = []
        for i in range(DPI_SLOT_COUNT):
            slot_col = QVBoxLayout()

            header_row = QHBoxLayout()
            radio = QRadioButton()
            self.active_group.addButton(radio, i)
            if i == 0:
                radio.setChecked(True)
            header_row.addWidget(radio)

            name_label = QLabel(f"Slot {i + 1}: 800 DPI")
            header_row.addWidget(name_label)
            header_row.addStretch()

            spin = QSpinBox()
            spin.setRange(50, 32000)
            spin.setSingleStep(50)
            spin.setValue(800)
            spin.setSuffix(" DPI")
            spin.setMinimumWidth(110)  # fits "32000 DPI" without squeezing/overlapping glyphs
            header_row.addWidget(spin)
            slot_col.addLayout(header_row)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(50, 32000)
            slider.setSingleStep(50)
            slider.setPageStep(500)
            slider.setValue(800)
            slot_col.addWidget(slider)

            # Bidirectional sync — Qt only emits valueChanged when the
            # value actually differs, so no manual re-entrancy guard
            # needed. Also keeps the "Slot N: XXXX DPI" label mirroring
            # the live value.
            def sync_label(value, label=name_label, slot_num=i + 1):
                label.setText(f"Slot {slot_num}: {value} DPI")
            spin.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(sync_label)

            dpi_layout.addLayout(slot_col)
            self.slot_spins.append(spin)
            self.slot_name_labels.append(name_label)

        self.apply_dpi_btn = QPushButton("Apply DPI Settings")
        self.apply_dpi_btn.clicked.connect(self.apply_dpi)
        dpi_layout.addWidget(self.apply_dpi_btn)

        layout.addWidget(dpi_box)
        layout.addStretch()
        return tab

    def _build_buttons_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        note = QLabel(
            "Each of the mouse's 6 buttons can be bound to a standard click, "
            "a DPI-cycle action, a fire-key action, a media key, or a keyboard "
            "shortcut (press the button below to capture it)."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        box = self._make_groupbox("Button Bindings")
        box_layout = QVBoxLayout(box)
        self.button_rows = []
        for i in range(BUTTON_SLOT_COUNT):
            row = ButtonSlotRow(i + 1)
            box_layout.addWidget(row)
            self.button_rows.append(row)
        layout.addWidget(box)

        btn_row = QHBoxLayout()
        self.apply_buttons_btn = QPushButton("Apply Bindings")
        self.apply_buttons_btn.clicked.connect(self.apply_buttons)
        btn_row.addWidget(self.apply_buttons_btn)
        self.reset_buttons_btn = QPushButton("Reset to Factory Defaults")
        self.reset_buttons_btn.clicked.connect(self.reset_buttons_action)
        btn_row.addWidget(self.reset_buttons_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addStretch()
        return tab

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
        for row in self.button_rows:
            row.set_dark_mode(checked)
        self.battery_indicator.set_dark_mode(checked)
        self._apply_themed_labels(checked)
        for box in self._groupboxes:
            self._style_groupbox(box, checked)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.reposition()

    # ---- background task helper ----

    def _run_bg(self, fn, on_done=None):
        # Only one command talks to the device at a time — running two
        # concurrently (e.g. clicking Apply DPI while a button-binding
        # write is still in flight) is exactly the kind of concurrent
        # access that's caused real corruption in testing.
        if self._busy:
            return
        self._busy = True
        self._set_controls_enabled(False)

        worker = Worker(fn)
        self._workers.append(worker)

        def cleanup():
            if worker in self._workers:
                self._workers.remove(worker)
            self._busy = False
            self._set_controls_enabled(True)

        if on_done:
            worker.done.connect(on_done)
        worker.error.connect(self._show_error)
        worker.finished.connect(cleanup)
        worker.start()

    def _set_controls_enabled(self, enabled):
        for btn in (self.refresh_btn, self.rate_apply_btn, self.apply_dpi_btn,
                    self.apply_buttons_btn, self.reset_buttons_btn):
            btn.setEnabled(enabled)

    def _show_error(self, msg):
        # Status bar only — no popup. A modal QMessageBox for every
        # transient read hiccup (e.g. a dropped 2.4G reply that would
        # have succeeded on the next Refresh anyway) is more disruptive
        # than the message itself; the status bar already surfaces it
        # clearly without blocking the rest of the UI.
        self.status_bar.showMessage(f"Error: {msg}")

    # ---- device actions ----

    def refresh_battery(self):
        def task():
            return run_isolated(get_battery_info)

        def done(info):
            self.battery_indicator.set_percent(info["battery_value"], bool(info["charge_flag"]))
            self.model_label.setText(f"EWEADN H2 ({info['device_id']})")
            self.fw_label.setText(f"Firmware: {info['firmware_version']}")
            self.ic_type = info["ic_type"]

        self._run_bg(task, done)

    def refresh(self):
        self.status_bar.showMessage("Reading current settings…")

        def task():
            # Each read gets its own connection — never chained on one
            # open handle. This used to open one connection and read
            # battery -> rate -> dpi -> buttons in sequence, which let
            # a leftover reply from one command bleed into the next
            # command's read; that's what caused the battery panel to
            # occasionally show a stale button-bindings reply's bytes
            # instead of the real battery percentage. See
            # run_isolated's docstring in h2_protocol.py.
            info = run_isolated(get_battery_info)
            time.sleep(0.1)
            rate = run_isolated(get_report_rate)
            time.sleep(0.1)
            dpi = run_isolated(get_dpi_config, ic_type=info["ic_type"])
            time.sleep(0.1)
            bindings = run_isolated(get_button_bindings)
            return info, rate, dpi, bindings

        def done(result):
            info, rate, dpi, bindings = result
            self.battery_indicator.set_percent(info["battery_value"], bool(info["charge_flag"]))
            self.model_label.setText(f"EWEADN H2 ({info['device_id']})")
            self.fw_label.setText(f"Firmware: {info['firmware_version']}")
            self.ic_type = info["ic_type"]

            self._select_rate(rate if rate in self.rate_buttons else 500)
            self.count_spin.setValue(dpi["dpi_count"])
            active_btn = self.active_group.button(dpi["dpi_index"])
            if active_btn:
                active_btn.setChecked(True)
            for spin, val in zip(self.slot_spins, dpi["levels"]):
                spin.setValue(val)

            for row, (type_, code1, code2) in zip(self.button_rows, bindings):
                row.set_from_binding(type_, code1, code2)

            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)

    def _select_rate(self, hz):
        """Checks the matching rate button, without sending anything to the device."""
        self._selected_rate = hz
        for rate, btn in self.rate_buttons.items():
            btn.setChecked(rate == hz)

    def apply_rate(self):
        hz = self._selected_rate
        self.status_bar.showMessage(f"Setting rate to {hz}Hz…")

        def task():
            run_isolated(set_report_rate, hz)

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
            run_isolated(set_dpi_config, levels, active, count, ic_type)

        def done(_):
            self.toast.show_message("DPI settings applied")
            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)

    def apply_buttons(self):
        bindings = [row.get_binding() for row in self.button_rows]

        unset_shortcuts = [
            ButtonSlotRow.SLOT_NAMES[row.slot_number - 1] for row in self.button_rows
            if row.type_combo.currentIndex() == 5 and row.key_capture.get_binding()[1] == 0
        ]
        if unset_shortcuts:
            QMessageBox.warning(
                self, "H2 Control",
                f"{', '.join(unset_shortcuts)} — set to Keyboard Shortcut but no key "
                f"has been captured yet — press \"Set…\" and press the combo first, "
                f"or change the type."
            )
            return

        self.status_bar.showMessage("Writing button bindings…")

        def task():
            # Write and read-back are two separate connections, not
            # one shared handle — same reasoning as refresh() above.
            run_isolated(set_button_bindings, bindings)
            time.sleep(0.1)
            return run_isolated(get_button_bindings)  # read back to confirm, same habit as the CLI

        def done(confirmed):
            for row, (type_, code1, code2) in zip(self.button_rows, confirmed):
                row.set_from_binding(type_, code1, code2)
            self.toast.show_message("Button bindings applied")
            self.status_bar.showMessage("Ready")

        self._run_bg(task, done)

    def reset_buttons_action(self):
        reply = QMessageBox.question(
            self, "H2 Control",
            "Restore all 6 buttons to their factory-default bindings?",
        )
        if reply != QMessageBox.Yes:
            return

        self.status_bar.showMessage("Restoring factory button defaults…")

        def task():
            run_isolated(reset_button_bindings)
            time.sleep(0.1)
            return run_isolated(get_button_bindings)

        def done(confirmed):
            for row, (type_, code1, code2) in zip(self.button_rows, confirmed):
                row.set_from_binding(type_, code1, code2)
            self.toast.show_message("Button bindings reset to factory defaults")
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
