# PyInstaller recipe for the standalone Laser Prepare app.
# macOS: app/build.sh makes "Laser Prepare.app"; Linux: app/build-linux.sh makes an AppImage from
# the "Laser Prepare" folder this produces.
import os
import sys

TOOLS = os.path.join(SPECPATH, '..', 'tools')
OPENMOL = os.path.join(SPECPATH, '..', 'OpenMOL', 'tools')    # the OpenMOL submodule: molwriter, leetro_usb, ...
BUILD = os.environ.get('LASER_BUILD', 'unknown build')      # git commit, set by the build scripts

# the build stamp travels inside the bundle (Info.plist only exists on macOS)
stamp = os.path.join(workpath, 'laser_build.txt')
os.makedirs(workpath, exist_ok=True)
with open(stamp, 'w') as f:
    f.write(BUILD)

hidden = ['openmol_path', 'svg2mol', 'dxf_load', 'molwriter', 'molreader', 'mol_inspect', 'leetro_usb', 'drawing_out', 'file_dialogs', 'linux_usb_access', 'sv_ttk',
          'usb.core', 'usb.util', 'usb.backend.libusb1', 'libusb_package']
if sys.platform.startswith('linux'):
    hidden += ['jeepney', 'jeepney.io.blocking']    # the desktop's file picker (file_dialogs.py)

a = Analysis([os.path.join(TOOLS, 'laser_window.py')], pathex=[TOOLS, OPENMOL],
             hiddenimports=hidden,
             datas=[(stamp, '.'), (os.path.join(SPECPATH, 'icon.png'), '.'),
                    (os.path.join(TOOLS, 'sv_ttk'), 'sv_ttk'),       # the dark theme's Tcl and images
                    # the udev rule the app installs on Linux (linux_usb_access.py): the same file as install-udev.sh's
                    (os.path.join(SPECPATH, '..', 'linux', '70-leetro-laser.rules'), 'linux')],
             excludes=['matplotlib', 'PIL', 'PyQt5', 'PySide6', 'IPython'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Laser Prepare', console=False,
          argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Laser Prepare')

if sys.platform == 'darwin':
    app = BUNDLE(
        coll, name='Laser Prepare.app', icon=os.path.join(SPECPATH, 'icon.icns'),
        bundle_identifier='org.laseradapter.prepare',
        info_plist={
            'CFBundleShortVersionString': '1.0',
            'LaserBuild': BUILD,
            'NSHighResolutionCapable': True,
            'CFBundleDocumentTypes': [
                {'CFBundleTypeName': 'SVG drawing', 'CFBundleTypeRole': 'Viewer', 'LSHandlerRank': 'Alternate',
                 'LSItemContentTypes': ['public.svg-image'], 'CFBundleTypeExtensions': ['svg']},
                {'CFBundleTypeName': 'DXF drawing', 'CFBundleTypeRole': 'Viewer', 'LSHandlerRank': 'Alternate',
                 'LSItemContentTypes': ['com.autodesk.dxf', 'public.data'], 'CFBundleTypeExtensions': ['dxf']},
                {'CFBundleTypeName': 'Leetro laser job', 'CFBundleTypeRole': 'Viewer', 'LSHandlerRank': 'Alternate',
                 'LSItemContentTypes': ['org.laseradapter.mol'], 'CFBundleTypeExtensions': ['mol', 'MOL']},
            ],
            # .MOL has no system type: declare one, so Open With and Dock drops offer the app for it
            'UTImportedTypeDeclarations': [
                {'UTTypeIdentifier': 'org.laseradapter.mol', 'UTTypeDescription': 'Leetro laser job',
                 'UTTypeConformsTo': ['public.data'],
                 'UTTypeTagSpecification': {'public.filename-extension': ['mol', 'MOL']}},
            ],
        })
