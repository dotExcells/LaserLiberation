# Developer notes

## How the pieces fit

- `tools/laser_window.py`: the Laser Prepare window (the app, and *Prepare for laser* in Inkscape).
- `tools/svg2mol.py`: SVG → `.MOL`, plus the shape loading and job building the window uses.
- `tools/dxf_load.py`: DXF reader (same shapes as the SVG reader). `tools/drawing_out.py`: writes
  shapes back out as SVG / DXF.
- `OpenMOL/tools/`: the `.MOL` writer and reader, USB sending (`leetro_usb.py`), `mol2dxf.py`,
  `mol_preview.py`, `mol_inspect.py`, and the format and USB notes.
- `inkscape/`: Inkscape extensions. `app/`: app builds. `linux/`: udev rule for USB access.

## Setting up a clone

**Tk version.** The window refuses a Tk known to drop clicks (`MIN_TK` in
`tools/laser_window.py`): on macOS it needs Tk 8.6.13 or newer (tested: Tk 9.0.4 from Homebrew
`python@3.14` + `python-tk@3.14`); elsewhere Tk 8.6.

macOS:
```bash
brew install python@3.14 python-tk@3.14
git clone https://github.com/dotExcells/LaserLiberation.git LaserLiberation
cd LaserLiberation
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/laser_window.py --check-tk
```

Linux (Debian / Ubuntu: `sudo apt install python3 python3-venv python3-tk git curl`;
Fedora: `sudo dnf install python3 python3-tkinter git curl`):
```bash
git clone https://github.com/dotExcells/LaserLiberation.git LaserLiberation
cd LaserLiberation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then:
```bash
.venv/bin/python tools/laser_window.py        # run the window from source
inkscape/install.sh                           # optional: Inkscape extensions (restart Inkscape)
app/build.sh                                  # macOS: build Laser Prepare.app into /Applications
app/build-linux.sh                            # Linux: AppImage in ~/Applications + menu entry
```

On Linux, Send to laser needs `linux/70-leetro-laser.rules` in `/etc/udev/rules.d/`: the app offers
to install it, or run `linux/install-udev.sh`; then replug the laser. Check with
`.venv/bin/python tools/laser_window.py --check-usb`. Build the AppImage on the oldest distribution
you'll use: it runs on newer systems, not older ones.

## Tests

```bash
.venv/bin/python tools/test_origin.py
.venv/bin/python tools/test_drawing_out.py
.venv/bin/python tools/test_bidir_shift.py
.venv/bin/python tools/test_file_dialogs.py
.venv/bin/python tools/test_linux_usb_access.py
.venv/bin/python OpenMOL/tools/test_origin.py
.venv/bin/python OpenMOL/tools/test_bidir_shift.py
```

Changes to how jobs are written should be tried on the machine at low power on scrap.
