## v1.1.0

### Fixed
- 🐛 Fixed a data-validation gap where a corrupted DPI-slot-count value could be silently accepted and displayed as if it were valid, instead of being caught and retried like every other read already is.

### New — CLI tool (`h2_battery.py`)
- Added button remapping: `--get-buttons`, `--set-button SLOT MOUSE_BUTTON`, `--reset-buttons` (standard mouse-button bindings — left/right/middle/back/forward)
- Added `--set-dpi-count` — a recovery flag for manually correcting the DPI slot count if it's ever wrong, without touching slot values

### Known limitations
- Button remapping is CLI-only for now — GUI support is planned for a future release
- Keyboard-shortcut and macro button bindings aren't supported (only standard mouse-button remapping)

---

**Full Changelog**: [v1.0.1...v1.1.0](../../compare/v1.0.1...v1.1.0)
