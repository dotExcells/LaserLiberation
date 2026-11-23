"""Make OpenMOL's modules (molwriter, molreader, leetro_usb, …) importable: they live in the
OpenMOL submodule (OpenMOL/tools). Import this before them. In the built app PyInstaller has
bundled them already."""
import os
import sys

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'OpenMOL', 'tools')

if os.path.isfile(os.path.join(TOOLS, 'molwriter.py')):
    if TOOLS not in sys.path:
        sys.path.insert(1, TOOLS)
elif not getattr(sys, 'frozen', False):
    sys.exit(f'OpenMOL is missing ({TOOLS}). Fetch it with:  git submodule update --init')
