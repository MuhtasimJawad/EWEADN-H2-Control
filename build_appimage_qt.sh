#!/usr/bin/env bash
# build_appimage_qt.sh — packages h2_gui_qt.py into EWEADN-H2-Control-x86_64.AppImage
#
# Run this ON YOUR OWN MACHINE (needs internet; a Wayland or X11 session
# for the build tools). Not something that can be built inside a
# sandboxed/offline environment.
#
# No system Qt/tk packages needed — PySide6 ships its own Qt binaries via pip.
#
# Usage:
#   chmod +x build_appimage_qt.sh
#   ./build_appimage_qt.sh

set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

APP_NAME="h2-control"
APPDIR="${WORKDIR}/AppDir"

echo "==> Setting up a build venv"
python3 -m venv .build-venv
source .build-venv/bin/activate
pip install --upgrade pip
pip install pyinstaller hidapi PySide6

echo "==> Freezing h2_gui_qt.py with PyInstaller"
pyinstaller --onefile --windowed \
    --name "${APP_NAME}" \
    --hidden-import hid \
    --collect-all hid \
    --collect-all PySide6 \
    h2_gui_qt.py

echo "==> Assembling AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"
cp "dist/${APP_NAME}" "${APPDIR}/usr/bin/${APP_NAME}"
chmod +x "${APPDIR}/usr/bin/${APP_NAME}"

cat > "${APPDIR}/${APP_NAME}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=EWEADN H2 Control
Comment=Battery, DPI, and polling rate control for the EWEADN H2 mouse
Exec=${APP_NAME}
Icon=${APP_NAME}
Categories=Utility;HardwareSettings;
Terminal=false
EOF

echo "==> Generating a simple placeholder icon (swap AppDir/${APP_NAME}.png for your own anytime)"
python3 - <<'PYEOF'
import struct, zlib, os

def write_png(path, width, height, rgb):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    row = bytes(rgb) * width
    raw = (b'\x00' + row) * height
    idat = zlib.compress(raw, 9)
    with open(path, 'wb') as f:
        f.write(sig)
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', idat))
        f.write(chunk(b'IEND', b''))

write_png(os.path.join("AppDir", "h2-control.png"), 256, 256, (66, 133, 244))
PYEOF

cat > "${APPDIR}/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/h2-control" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

echo "==> Fetching appimagetool (only downloaded once, cached in this folder)"
if [ ! -f appimagetool-x86_64.AppImage ]; then
    curl -L -o appimagetool-x86_64.AppImage \
        https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x appimagetool-x86_64.AppImage
fi

echo "==> Building the AppImage"
ARCH=x86_64 ./appimagetool-x86_64.AppImage "${APPDIR}" "EWEADN-H2-Control-x86_64.AppImage"

deactivate
echo ""
echo "Done: ./EWEADN-H2-Control-x86_64.AppImage"
echo "Run it with: chmod +x EWEADN-H2-Control-x86_64.AppImage && ./EWEADN-H2-Control-x86_64.AppImage"
