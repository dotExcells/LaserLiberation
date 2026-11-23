#!/bin/sh
# Build "Laser Prepare.app" into app/dist and copy it to /Applications.
set -e
if [ "$(uname)" != "Darwin" ]; then
  echo "Laser Prepare.app can only be built on macOS." >&2
  exit 1
fi
cd "$(dirname "$0")"
# OpenMOL (molwriter, molreader, leetro_usb, ...) is a git submodule: fetch it if it's missing
if [ ! -f ../OpenMOL/tools/molwriter.py ]; then
  git -C .. submodule update --init || true
  if [ ! -f ../OpenMOL/tools/molwriter.py ]; then
    echo "OpenMOL is missing. In the LaserFreedom folder run:  git submodule update --init" >&2
    exit 1
  fi
fi
case "$(git -C .. submodule status OpenMOL 2>/dev/null)" in
  +*) echo "Note: OpenMOL/ isn't at the commit LaserFreedom records (git submodule update brings it there)." ;;
esac
# refuse to build from a Python whose Tk drops clicks on macOS (see DEVELOPMENT.md)
../.venv/bin/python ../tools/laser_window.py --check-tk || exit 1
# stamp the build with the commit it comes from (+changes if tools/, app/ or OpenMOL/ differ from it)
LASER_BUILD="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git status --porcelain -- ../tools ../app ../OpenMOL 2>/dev/null)" ]; then
  LASER_BUILD="$LASER_BUILD+changes"
fi
export LASER_BUILD
echo "Building Laser Prepare ($LASER_BUILD)"
../.venv/bin/pyinstaller --noconfirm --clean --log-level WARN "Laser Prepare.spec"
rm -rf "/Applications/Laser Prepare.app"
cp -R "dist/Laser Prepare.app" /Applications/
echo "Installed /Applications/Laser Prepare.app"
