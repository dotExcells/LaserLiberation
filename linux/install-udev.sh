#!/bin/sh
# Install the udev rule that lets you send jobs to the laser over USB without root (Linux only).
# Run once: linux/install-udev.sh   (asks for your password via sudo), then replug the laser.
set -e
if [ "$(uname)" != "Linux" ]; then
  echo "This is for Linux; macOS needs no rule." >&2
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
sudo install -m 0644 "$HERE/70-leetro-laser.rules" /etc/udev/rules.d/70-leetro-laser.rules
sudo rm -f /etc/udev/rules.d/99-leetro-laser.rules   # old name, too late for uaccess
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=0548
echo "Installed /etc/udev/rules.d/70-leetro-laser.rules. Unplug and replug the laser, then check with:"
echo "  .venv/bin/python tools/laser_window.py --check-usb"
