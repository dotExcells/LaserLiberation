#!/bin/sh
# Build Laser Prepare as one clickable Linux file (an AppImage) and add it to the applications menu.
#
#   app/build-linux.sh               build, then install for this user
#   app/build-linux.sh --no-install  only build (used by the GitHub release builds)
#
# Makes app/dist/Laser_Prepare-x86_64.AppImage, copies it to ~/Applications and adds a menu entry
# with the icon. The project's .venv is created on the first run; it is only needed for building,
# never for using the app. Build on the oldest Linux you'll use (Debian rather than Fedora): an
# AppImage runs on newer systems than the one it was built on, not on older ones.
set -e
INSTALL=1
case "$1" in
  --no-install) INSTALL=0 ;;
  "") ;;
  *) echo "Usage: $0 [--no-install]" >&2; exit 1 ;;
esac
if [ "$(uname)" != "Linux" ]; then
  echo "This builds the Linux app; on macOS use app/build.sh." >&2
  exit 1
fi
if [ "$(uname -m)" != "x86_64" ]; then
  echo "Only x86_64 (normal PCs) is set up so far; this is $(uname -m)." >&2
  exit 1
fi
cd "$(dirname "$0")"
TOP="$(cd .. && pwd)"

# --- OpenMOL (molwriter, molreader, leetro_usb, ...): a git submodule, or already in place in a
# source package
if [ ! -f "$TOP/OpenMOL/tools/molwriter.py" ]; then
  if command -v git >/dev/null 2>&1 && git -C "$TOP" rev-parse >/dev/null 2>&1; then
    git -C "$TOP" submodule update --init || true
  fi
  if [ ! -f "$TOP/OpenMOL/tools/molwriter.py" ]; then
    echo "OpenMOL is missing. In the LaserFreedom folder run:  git submodule update --init" >&2
    exit 1
  fi
fi

# --- what the build needs from the system (asked for once, with the command for this Linux)
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
python3 -c 'import tkinter' 2>/dev/null || need="$need tkinter"
python3 -c 'import venv, ensurepip' 2>/dev/null || need="$need venv"
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || need="$need curl"
if [ -n "$need" ]; then
  echo "Missing for the build:$need. Install with:" >&2
  if command -v apt >/dev/null 2>&1; then
    echo "  sudo apt install python3 python3-venv python3-tk curl" >&2
  elif command -v dnf >/dev/null 2>&1; then
    echo "  sudo dnf install python3 python3-tkinter curl" >&2
  else
    echo "  python3 with tkinter and venv, curl (your distribution's packages)" >&2
  fi
  exit 1
fi

# --- build environment (only for building)
if [ ! -x "$TOP/.venv/bin/python" ]; then
  echo "Creating the build environment (.venv), once"
  python3 -m venv "$TOP/.venv"
fi
"$TOP/.venv/bin/pip" install -q -r "$TOP/requirements.txt"
"$TOP/.venv/bin/python" "$TOP/tools/laser_window.py" --check-tk

# --- stamp with the commit (+changes if tools/, app/ or OpenMOL/ differ from it), like build.sh;
# a source package has no git, but records its commit in BUILD_COMMIT; a LASER_BUILD already set
# (the release version, in the GitHub builds) wins
if [ -n "$LASER_BUILD" ]; then
  :
elif command -v git >/dev/null 2>&1 && git rev-parse --short HEAD >/dev/null 2>&1; then
  LASER_BUILD="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain -- ../tools ../app ../OpenMOL 2>/dev/null)" ]; then
    LASER_BUILD="$LASER_BUILD+changes"
  fi
elif [ -f BUILD_COMMIT ]; then
  LASER_BUILD="$(cat BUILD_COMMIT)"
else
  LASER_BUILD="unknown"
fi
export LASER_BUILD
echo "Building Laser Prepare ($LASER_BUILD)"
"$TOP/.venv/bin/pyinstaller" --noconfirm --clean --log-level WARN "Laser Prepare.spec"

# --- AppImage: the bundle plus a start script, menu entry and icon
APPDIR="build/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -R "dist/Laser Prepare" "$APPDIR/usr/lib/laser-prepare"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/laser-prepare/Laser Prepare" "$@"
EOF
chmod +x "$APPDIR/AppRun"
cat > "$APPDIR/laser-prepare.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Laser Prepare
Comment=Prepare SVG/DXF drawings for the Leetro laser
Exec=laser-prepare %f
Icon=laser-prepare
Categories=Graphics;
MimeType=image/svg+xml;image/vnd.dxf;application/x-leetro-mol;
Terminal=false
EOF
cp icon.png "$APPDIR/laser-prepare.png"
cp icon.png "$APPDIR/.DirIcon"

# appimagetool (the AppImage project's own builder), fetched once into app/build/
TOOL="build/appimagetool-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  echo "Fetching appimagetool, once"
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$TOOL" "$URL"; else wget -q -O "$TOOL" "$URL"; fi
  chmod +x "$TOOL"
fi
OUT="dist/Laser_Prepare-x86_64.AppImage"
rm -f "$OUT"
# extract-and-run: works without FUSE on the build machine
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" "$OUT" >/dev/null
echo "Built app/$OUT"
[ "$INSTALL" = 1 ] || exit 0

# --- install for this user: ~/Applications plus a menu entry with the icon
mkdir -p "$HOME/Applications" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/256x256/apps"
cp "$OUT" "$HOME/Applications/Laser_Prepare-x86_64.AppImage"
chmod +x "$HOME/Applications/Laser_Prepare-x86_64.AppImage"
cp icon.png "$HOME/.local/share/icons/hicolor/256x256/apps/laser-prepare.png"
sed -e "s|^Exec=.*|Exec=\"$HOME/Applications/Laser_Prepare-x86_64.AppImage\" %f|" \
    -e "s|^Icon=.*|Icon=$HOME/.local/share/icons/hicolor/256x256/apps/laser-prepare.png|" \
    "$APPDIR/laser-prepare.desktop" > "$HOME/.local/share/applications/laser-prepare.desktop"
# .MOL jobs have no standard MIME type: add one (by name, and by the header's version word, as
# *.mol is also a chemistry format), so the file manager offers Laser Prepare for them
mkdir -p "$HOME/.local/share/mime/packages"
cat > "$HOME/.local/share/mime/packages/laser-prepare.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-leetro-mol">
    <comment>Leetro laser job</comment>
    <glob pattern="*.mol"/>
    <magic priority="60"><match type="little32" offset="8" value="0x04030000"/></magic>
  </mime-type>
</mime-info>
EOF
command -v update-mime-database >/dev/null 2>&1 && update-mime-database "$HOME/.local/share/mime" || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$HOME/.local/share/applications" || true
echo "Installed ~/Applications/Laser_Prepare-x86_64.AppImage; Laser Prepare is in your applications menu."
