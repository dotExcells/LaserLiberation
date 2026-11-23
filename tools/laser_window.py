#!/usr/bin/env python3
""""Prepare for laser": open an SVG or DXF drawing, set each colour's laser settings, save .MOL
to a USB stick or a file, or send it straight to the laser over its USB cable. Shapes can be
recoloured in the preview (click or drag to select) when the drawing's colours aren't set up,
and the recoloured drawing saved as a new SVG or DXF. A .MOL job (LaserCut's or ours) opens
read-only, with its preview and settings shown, and is sent or saved byte for byte as it is.

Usage: laser_window.py [drawing.svg|drawing.dxf|job.MOL] [--title NAME]

Runs as the standalone Laser Prepare app (no file: shows an Open button; files can also be
dropped on the app icon) and from Inkscape (Extensions > Laser > Prepare for laser...).
Every fill colour becomes an engrave row and every outline colour a cut row; an outline colour
with closed shapes can be switched to engrave its insides instead (handy for DXF, which has no
fills). Settings are remembered per colour in ~/Library/Application Support/Laser Prepare (macOS),
%APPDATA%\\Laser Prepare (Windows) or ~/.config/laser-prepare (Linux).
"""
import argparse
import colorsys
import contextlib
import functools
import hashlib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(__file__))
import openmol_path  # noqa: E402,F401  (puts OpenMOL/tools on the path)
import svg2mol  # noqa: E402
import dxf_load  # noqa: E402
import leetro_usb  # noqa: E402
import linux_usb_access  # noqa: E402
import drawing_out  # noqa: E402
import file_dialogs  # noqa: E402
import molreader  # noqa: E402
from molwriter import STEPS_PER_MM, CutLayer  # noqa: E402

APP = 'Laser Prepare'


def build_label():
    """Which code this is: the git commit an app build was made from, or 'source'."""
    if getattr(sys, 'frozen', False):
        try:
            with open(os.path.join(getattr(sys, '_MEIPASS', ''), 'laser_build.txt')) as f:
                return f.read().strip() or 'unknown build'
        except OSError:
            pass
        info = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), 'Info.plist')
        try:
            with open(info, 'rb') as f:
                return plistlib.load(f).get('LaserBuild', 'unknown build')
        except OSError:
            return 'unknown build'
    return 'source'


TITLE = f'{APP} ({build_label()})'
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.platform == 'darwin':
    SETTINGS_DIR = os.path.expanduser(f'~/Library/Application Support/{APP}')
elif sys.platform == 'win32':
    SETTINGS_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), APP)
else:
    SETTINGS_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'laser-prepare')
MEMORY = os.path.join(SETTINGS_DIR, 'remembered.json')
OLD_MEMORY = os.path.join(PROJECT, 'settings', 'remembered.json')
NEW_COLOUR = {
    'engrave': {'speed': 500.0, 'power': 30.0},
    'cut': {'speed': 10.0, 'power': 30.0, 'corner': 30.0},
}
FIELDS = {'engrave': ('speed', 'power', 'interval'), 'cut': ('speed', 'power', 'corner')}
COLUMNS = ('speed', 'power', 'corner', 'interval')          # number columns in the colour table
LIMITS = {'speed': (1, 1000), 'power': (0, 100), 'corner': (0, 100), 'interval': (0.02, 2)}
FIELD_NAME = {'interval': 'line spacing mm'}                   # for error messages
# colour table headings over the number boxes: (name, unit)
COLUMN_NAMES = {'speed': ('Speed', 'mm/s'), 'power': ('Power', '%'), 'corner': ('Corner\npower', '%'),
                'interval': ('Line\nspacing', 'mm')}            # two lines: no wider than their boxes
TABLE_HEAD_FONT = ('TkDefaultFont', 9, 'bold')   # colour table headings
INTERVAL = 0.1              # line spacing for a new colour when no old global value is saved
SHORT_NAME = re.compile(r'[A-Z0-9_]{1,8}')
JOBS = {'Cut': 'cut', 'Engrave': 'engrave'}                         # outline colours (Engrave: their insides)
FILL_JOBS = {'Engrave': 'engrave', 'Cut': 'cut'}                     # filled colours (Cut: their edges)
OUTLINE = ' (outline)'      # suffix for engrave layers made from an outline colour
EDGE = ' (edge)'            # suffix for cut layers made from the edges of a filled colour
# where the head is when the job starts (Settings); svg2mol.OFFERED_ORIGINS, as said under the preview
ORIGIN_WORDS = {'top-left': 'top-left', 'top-right': 'top-right', 'bottom-left': 'bottom-left',
                'bottom-right': 'bottom-right', 'center': 'centre'}


def ink(colour):
    """The real colour of a layer name that may carry a suffix."""
    return colour.replace(OUTLINE, '').replace(EDGE, '')
FILETYPES = [('Drawings and .MOL jobs', '*.svg *.SVG *.dxf *.DXF *.mol *.MOL'), ('SVG', '*.svg'), ('DXF', '*.dxf'),
             ('Leetro job', '*.mol *.MOL')]
# The window is always dark (Sun Valley dark ttk theme, tools/sv_ttk), on macOS and Linux alike.
# Colours of everything the theme doesn't draw:
COLOURS = {
    'bg': '#1c1c1c',            # window background (the theme's)
    'hint': '#d0d0d0',          # grey help text (the only text colour besides the theme's, warn and error)
    'warn': '#ffb454',          # skipped-shapes notes
    'error': '#ff6b6b',         # bad numbers in the table
    'canvas': '#262626',        # the preview
    'canvas_edge': '#4a4a4a',
    'faint': '#4a4a4a',         # the whole drawing, under what will run
    'start': '#ff3b30',         # the start point
    'swatch': '#d8d8d8',        # behind the colour boxes, so dark inks show as they are
    'swatch_edge': '#888888',
    'tip': '#2b2b2b',           # behind a mouse-over note (a colour's hex code)
}
INK_CONTRAST = 3.0          # the preview draws inks darker than this against its background lighter
SELECTED = '#0a84ff'        # box-selection rectangle in the preview
GLOW = '#8ec5ff'            # selected shapes get a light-blue glow behind them, keeping their own colour
PALETTE = ['#000000', '#ff0000', '#0000ff', '#00a000', '#ff8000', '#a000ff',      # recolour boxes
           '#00b0b0', '#ff00ff', '#808000', '#804000', '#808080', '#d0a000']
MOL_COLOURS = PALETTE[1:]   # an opened .MOL's layers in the preview and summary, in run order
CLICK_PX = 6                # how close (screen pixels) a click must be to a line to pick it
DRAG_PX = 8                 # a click may move this much (trackpads wobble) before it counts as a drag
ZOOM_MAX = 50               # the preview zooms in up to 50x the whole-drawing view
ZOOM_PX_PER_MM = 100        # or to 0.01 mm a pixel, whichever comes first (always at least 4x)
ZOOM_STEP = 1.25            # + / - keys
ZOOM_REDRAW_MS = 120        # zooming and panning move what is drawn at once, then draw it afresh


# Tk 8.6.12 and older drop mouse clicks on newer macOS until the window loses and regains focus
# (seen on macOS 26). Tested: Tk 9.0.4 (Homebrew python-tk@3.14). Elsewhere any Tk 8.6 works.
MIN_TK = (8, 6, 13) if sys.platform == 'darwin' else (8, 6, 0)


def tk_version(root):
    return tuple(int(p) for p in re.findall(r'\d+', root.tk.call('info', 'patchlevel'))[:3])


def tk_problem(root):
    """A message if this Tk is too old for the window, else None."""
    have = tk_version(root)
    if have >= MIN_TK:
        return None
    need = '.'.join(map(str, MIN_TK))
    fix = ('Rebuild the project\'s .venv with Homebrew\'s Python 3.14 (brew install python@3.14 '
           'python-tk@3.14), see DEVELOPMENT.md.' if sys.platform == 'darwin'
           else 'Install a newer python3-tk and rebuild the project\'s .venv, see DEVELOPMENT.md.')
    return (f'This Python has Tk {".".join(map(str, have))}, but {APP} needs Tk {need} or newer here '
            f'(older Tk drops mouse clicks).\n\n{fix}')


def dark_theme(root):
    """Sun Valley dark on every widget, with Tk's own fonts kept (the theme would switch to Segoe UI)
    and narrower spin box arrows (the theme's take ~55 px more per box, widening the window). The
    arrows are clam's, drawn as shapes (crisp at any scaling, unlike the theme's 10 x 5 px images),
    on the colour of the box's inside in each state, and hidden in a disabled (empty) box."""
    import sv_ttk
    sv_ttk.set_theme('dark', root)
    root.tk.eval('''
        font configure SunValleyBodyFont {*}[font configure TkDefaultFont]
        ttk::style theme settings sun-valley-dark {
            ttk::style configure . -font TkDefaultFont
            ttk::style element create Narrow.uparrow from clam Spinbox.uparrow
            ttk::style element create Narrow.downarrow from clam Spinbox.downarrow
            ttk::style layout TSpinbox {
                Spinbox.field -side top -sticky we -children {
                    null -side right -sticky ns -children {
                        Narrow.uparrow -side top -sticky nse
                        Narrow.downarrow -side bottom -sticky nse
                    }
                    Spinbox.padding -sticky nswe -children {Spinbox.textarea -sticky nsew}
                }
            }
            # the inside of the theme's text box images: rest #292929, then by state as they switch
            set spin_inside {focus #1c1c1c invalid #1c1c1c disabled #262626 hover #2f2f2f}
            ttk::style configure TSpinbox -arrowsize 9 -arrowcolor #c8c8c8 -background #292929 \\
                -bordercolor #292929 -lightcolor #292929 -darkcolor #292929
            ttk::style map TSpinbox -background $spin_inside -bordercolor $spin_inside -lightcolor $spin_inside \\
                -darkcolor $spin_inside -arrowcolor {disabled #262626 pressed #8a8a8a}
            # the colour table's job menus: less room around the text and before the arrow. Set
            # here, before any widget exists: changing a style later makes sv_ttk repaint plain Tk
            # widgets (the colour swatches) with the theme's palette.
            ttk::style configure Job.TMenubutton -padding {6 2 4 3}
        }''')
    root.configure(background=COLOURS['bg'])
    if root.tk.call('tk', 'windowingsystem') == 'aqua':
        # dark title bar and native pop-up menus, whatever the system setting
        with contextlib.suppress(tk.TclError):
            root.tk.call('::tk::unsupported::MacWindowStyle', 'appearance', root, 'darkaqua')


def luminance(colour):
    """Relative luminance (WCAG) of '#rrggbb'."""
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(colour[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def hover_tip(widget, text):
    """Show `text` in a small box next to the mouse while it is over `widget` (macOS and Linux)."""
    tip = None

    def hide(e=None):
        nonlocal tip
        if tip is not None:
            with contextlib.suppress(tk.TclError):
                tip.destroy()
            tip = None

    def show(e):
        nonlocal tip
        hide()
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        if widget.tk.call('tk', 'windowingsystem') == 'aqua':
            with contextlib.suppress(tk.TclError):      # a help-tag window that never takes focus
                widget.tk.call('::tk::unsupported::MacWindowStyle', 'style', tip._w, 'help', 'noActivates')
        tk.Label(tip, text=text, background=COLOURS['tip'], foreground=COLOURS['hint'], padx=6, pady=2,
                 highlightthickness=1, highlightbackground=COLOURS['swatch_edge']).pack()
        tip.wm_geometry(f'+{e.x_root + 12}+{e.y_root + 16}')
    for seq, f in (('<Enter>', show), ('<Leave>', hide), ('<ButtonPress>', hide), ('<Destroy>', hide)):
        widget.bind(seq, f, add='+')


@functools.lru_cache(maxsize=None)
def screen_ink(colour, background=COLOURS['canvas']):
    """How the preview shows an ink: as it is, unless too dark to see on the dark background. Then
    its lightness is mirrored (black shows white, navy light blue, as CAD programs do on dark) and
    raised further if needed, keeping the hue. Only the screen changes, never what is saved or cut."""
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', colour):
        return colour
    bg = luminance(background)

    def contrast(c):
        return (luminance(c) + 0.05) / (bg + 0.05)
    if contrast(colour) >= INK_CONTRAST:
        return colour
    h, l, s = colorsys.rgb_to_hls(*(int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    l = max(l, 1 - l)
    while True:
        out = '#%02x%02x%02x' % tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, l, s))
        if contrast(out) >= INK_CONTRAST or l >= 1:
            return out
        l = min(1.0, l + 0.05)


def load_design(path):
    """cuts, fills, skipped for an SVG or DXF file."""
    if path.lower().endswith('.dxf'):
        return dxf_load.load(path)
    return svg2mol.load(path)


def is_mol(path):
    return path.lower().endswith('.mol')


def mm_text(v):
    """A number from a .MOL for the summary: steps/s and steps come back as 9.999 or 0.099."""
    return f'{v:.2f}'.rstrip('0').rstrip('.') if v < 1 else f'{v:.1f}'.rstrip('0').rstrip('.')


def load_mol(path):
    """A .MOL job to send as it is: its bytes and, when molreader can read it, what it does (for
    the preview and summary only; the bytes are never rebuilt). Raises leetro_usb.LeetroError if
    it isn't a whole .MOL file (the check sending makes), OSError if it can't be read."""
    with open(path, 'rb') as f:
        data = f.read()
    leetro_usb.check_job(data)
    mol = {'path': path, 'data': data, 'sha256': hashlib.sha256(data).digest(), 'layers': None}
    try:
        layers, _ = molreader.extract(data)
        x0, y0, x1, y1 = molreader.job_box(data)
        origin = molreader.job_origin(data)
        seconds = struct.unpack_from('<I', data, 48)[0]      # header word 12
    except Exception:           # another LaserCut version, say: it can still be sent as it is
        return mol
    if not layers:
        return mol
    S, out = STEPS_PER_MM, []
    xs, ys = [x0, x1, 0], [y0, y1, 0]           # the job box and the head
    for i, layer in enumerate(layers):
        entry = {'key': f'layer {i + 1}', 'colour': MOL_COLOURS[i % len(MOL_COLOURS)],
                 'speed': layer.speed / S, 'power': layer.power / 100}
        if isinstance(layer, CutLayer):
            entry.update(kind='cut', corner=layer.corner_power / 100,
                         paths=[[(x / S, y / S) for x, y in p] for p in layer.paths])
            xs += [x for p in layer.paths for x, _ in p]
            ys += [y for p in layer.paths for _, y in p]
        else:
            steps = [a - b for (a, _), (b, _) in zip(layer.lines, layer.lines[1:]) if a > b]
            entry.update(kind='engrave', bidir=layer.bidirectional,
                         spacing=min(steps) / S if steps else None,
                         lines=[(y / S, [(a / S, b / S) for a, b in spans]) for y, spans in layer.lines])
            xs += [x for _, spans in layer.lines for span in spans for x in span]
            ys += [y for y, _ in layer.lines]
        out.append(entry)
    mol.update(layers=out, origin=origin, seconds=seconds, size=((x1 - x0) / S, (y1 - y0) / S),
               box=(min(xs) / S, min(ys) / S, max(xs) / S, max(ys) / S),
               points=sum(len(p) for e in out for p in e.get('paths', ())) + sum(
                   len(spans) for e in out for _, spans in e.get('lines', ())))
    return mol


def closed(poly):
    return len(poly) > 3 and poly[0] == poly[-1]


def segment_distance(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


BUSY_POINTS = 20000     # estimates and redraws of drawings smaller than this take under ~0.1 s


def busy_cursor(always=True):
    """Run the method with the busy cursor showing (see LaserWindow.busy)."""
    def wrap(method):
        @functools.wraps(method)
        def run(self, *args, **kwargs):
            with self.busy(always):
                return method(self, *args, **kwargs)
        return run
    return wrap


# ---------------------------------------------------------------- remembered settings

def load_memory():
    if not os.path.exists(MEMORY) and os.path.exists(OLD_MEMORY):
        os.makedirs(os.path.dirname(MEMORY), exist_ok=True)
        shutil.copy(OLD_MEMORY, MEMORY)
    try:
        with open(MEMORY) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for k in ('engrave', 'cut', 'options'):
        data.setdefault(k, {})
    return data


def save_memory(data):
    os.makedirs(os.path.dirname(MEMORY), exist_ok=True)
    tmp = MEMORY + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, MEMORY)


# ---------------------------------------------------------------- USB stick

def usb_sticks():
    """Mounted external/removable volumes, as (name, path). macOS only; elsewhere use Save as file.
    Only asked when you click Save to USB stick, so nothing runs in the background while you work."""
    if sys.platform != 'darwin':
        return []
    found = []
    for name in sorted(os.listdir('/Volumes')):
        path = os.path.join('/Volumes', name)
        if not os.path.ismount(path):
            continue
        try:
            out = subprocess.run(['diskutil', 'info', '-plist', path], capture_output=True, timeout=5).stdout
            info = plistlib.loads(out)
        except Exception:
            continue
        if info.get('Internal') is False or info.get('RemovableMediaOrExternalDevice') or \
                info.get('BusProtocol') == 'USB':
            found.append((name, path))
    return found


def clean_stick(path):
    """Remove macOS's hidden ._ files, which the controller lists as extra entries."""
    subprocess.run(['dot_clean', '-m', path], capture_output=True)
    for entry in os.listdir(path):
        if entry.startswith('._'):
            try:
                os.remove(os.path.join(path, entry))
            except OSError:
                pass


# ---------------------------------------------------------------- window

class LaserWindow:
    def __init__(self, root, path=None, title=None):
        self.root = root
        self.memory = load_memory()
        self.body = None
        self._estimate_job = None
        self._redraw_job = None
        self._preview = None            # what the preview last drew: draw_preview's arguments
        self._zoom = None               # None: the whole drawing; else (px per mm, centre x, y in mm)
        self._zoom_job = None
        self._pan = None                # (x, y) of the pointer while dragging the view
        self._space = False             # Space held down over the preview: left drag pans
        self._busy, self._points = False, 0
        self.settings_window = None
        self.mol = None                 # an opened .MOL job (load_mol), sent as it is; None for drawings
        # Settings (machine-wide, not per colour): changed in the Settings window, kept in options
        o = self.memory['options']
        self.bidir = tk.BooleanVar(value=bool(o.get('bidir', True)))
        origin = o.get('origin')
        # an old 'center' (no longer offered) or anything unknown starts from the top-left again
        self.origin = tk.StringVar(value=origin if origin in svg2mol.OFFERED_ORIGINS else 'top-left')
        # start, size and Frame from the whole drawing, unticked colours too (like LaserCut)
        self.whole = tk.BooleanVar(value=bool(o.get('whole', False)))
        root.title(TITLE)
        root.minsize(1000, 560)

        bar = ttk.Frame(root, padding=(14, 10, 14, 0))
        bar.pack(fill='x')
        # Hidden on the start screen, which has its own Open button; shown once a drawing is open.
        self.open_button = ttk.Button(bar, text='Open drawing…', command=self.ask_open)
        self.open_button.pack(side='left')
        # Like Open drawing…: hidden on the start screen, shown once a file is open.
        self.settings_button = ttk.Button(bar, text='Settings…', command=self.show_settings)
        self.settings_button.pack(side='right')
        self.file_label = ttk.Label(bar, text='', font=('TkDefaultFont', 13, 'bold'))
        self.file_label.pack(side='left', padx=12)
        self.size_label = ttk.Label(bar, text='')
        self.size_label.pack(side='left')

        if path:
            self.open(path, title)
        else:
            self.welcome()

    # ------------------------------------------------------------ opening

    def new_body(self):
        if self.body is not None:
            self.body.destroy()
        self.body = ttk.Frame(self.root, padding=14)
        self.body.pack(fill='both', expand=True)
        self.usb_button = None
        self.laser_button = None
        return self.body

    def welcome(self):
        self.open_button.pack_forget()
        self.settings_button.pack_forget()
        body = self.new_body()
        box = ttk.Frame(body)
        box.place(relx=0.5, rely=0.45, anchor='center')
        ttk.Label(box, text='Open an SVG or DXF drawing', font=('TkDefaultFont', 18)).pack(pady=6)
        ttk.Label(box, text='or drop one on the Laser Prepare icon in the Dock.\n\n'
                            'Fill colours are engraved, outline colours are cut.\n'
                            'DXF lines are cut; switch a colour to "Engrave" to fill it.\n'
                            'A .MOL job opens as it is, to send it to the laser unchanged.',
                  justify='center', foreground=COLOURS['hint']).pack(pady=6)
        ttk.Button(box, text='Open drawing…', command=self.ask_open).pack(pady=10)

    def show_toolbar(self):
        """The toolbar's Open drawing… and Settings… buttons, hidden on the start screen."""
        if not self.open_button.winfo_manager():
            self.open_button.pack(side='left', before=self.file_label)
        if not self.settings_button.winfo_manager():
            self.settings_button.pack(side='right')

    def ask_open(self):
        o = self.memory['options']
        path = file_dialogs.askopenfilename(parent=self.root, filetypes=FILETYPES,
                                            initialdir=o.get('open_dir') or os.path.expanduser('~'))
        if path:
            o['open_dir'] = os.path.dirname(path)
            self.open(path)

    def open(self, path, title=None):
        if self.body is not None and hasattr(self, 'rows'):
            try:
                self.remember()
            except Exception:
                pass
        if is_mol(path):
            return self.open_mol(path, title)
        try:
            with self.busy():
                self.cuts, self.fills, self.skipped = load_design(path)
        except Exception as e:
            messagebox.showerror(APP, f'Could not read {os.path.basename(path)}:\n\n{e}', parent=self.root)
            if self.body is None:
                self.welcome()
            return
        self.show_toolbar()
        self.path, self.rows, self.mol = path, {}, None
        self.selection, self.history = set(), []
        self._boxes = {}
        self._zoom = self._pan = None           # a new drawing starts whole
        name = title or os.path.basename(path)
        self.root.title(f'{TITLE}: {name}')
        self.file_label.configure(text=name)
        self.size_label.configure(text='')
        with self.busy():
            self.count_points()
            self.order = svg2mol.run_order(self.cuts, self.fills)
            self.build_ui()
        if self.settings_window is not None:        # open Settings follow the new file
            self.fill_settings()

    def open_mol(self, path, title=None):
        """Open a .MOL job read-only: its preview and settings, to send or save it as it is."""
        name = title or os.path.basename(path)
        try:
            with self.busy():
                mol = load_mol(path)
        except leetro_usb.LeetroError as e:
            why = str(e).replace('not sending: ', '')
            messagebox.showerror(APP, f'{name} is not a .MOL job that can be sent to the laser:\n\n{why}.',
                                 parent=self.root)
            mol = None
        except OSError as e:
            messagebox.showerror(APP, f'Could not read {name}:\n\n{e}', parent=self.root)
            mol = None
        if mol is None:
            if self.body is None:
                self.welcome()
            return
        self.show_toolbar()
        self.path, self.mol, self.rows, self.order = path, mol, {}, []
        self.cuts, self.fills, self.skipped = {}, {}, {}
        self.selection, self.history, self._boxes = set(), [], {}
        self._zoom = self._pan = self._preview = None
        self.view = self._fit = None
        self._points = mol.get('points', 0)
        self.root.title(f'{TITLE}: {name}')
        self.file_label.configure(text=name)
        size = mol.get('size')
        self.size_label.configure(text=f'Design size: {size[0]:.1f} x {size[1]:.1f} mm' if size else '')
        with self.busy():
            self.build_mol_ui()
        if self.settings_window is not None:        # open Settings follow the new file
            self.fill_settings()

    def build_mol_ui(self):
        """A .MOL's preview and a read-only summary of its layers; nothing to change."""
        mol, layers = self.mol, self.mol['layers']
        body = self.new_body()
        left = self.preview_area(body)
        self.sel_label = None
        if layers:
            bar = ttk.Frame(left)
            bar.pack(fill='x', pady=(6, 0))
            self.zoom_bar(bar)
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew', padx=(14, 0))
        stem = os.path.splitext(os.path.basename(self.path))[0].upper()
        self.save_area(right, stem if SHORT_NAME.fullmatch(stem) else None)
        secs = mol.get('seconds')
        self.estimate.configure(text=f'Estimated time (from the file): {secs // 60} min {secs % 60:02d} s'
                                if layers else '')

        self.wrapped = []
        panel = self.scroll_panel(right)
        self.wrap_label(ttk.Label(panel, text='This .MOL job is sent and saved exactly as it is, with its own '
                                              'settings and start position (Settings… shows them, read-only).',
                                  foreground=COLOURS['hint'])).pack(anchor='w', fill='x')
        self.table = ttk.Frame(panel)
        self.table.pack(anchor='w', pady=(4, 6))
        if layers:
            self.fill_mol_table(layers)
            self._preview = ({e['key']: e['paths'] for e in layers if e['kind'] == 'cut'},
                             {e['key']: None for e in layers if e['kind'] == 'engrave'},
                             {e['key']: e['spacing'] or 1.0 for e in layers if e['kind'] == 'engrave'},
                             {e['key']: e['lines'] for e in layers if e['kind'] == 'engrave'}, (0.0, 0.0))
        else:
            self.wrap_label(ttk.Label(panel, text='Laser Prepare can\'t read what this file does (made by '
                                                  'another LaserCut version?), so it shows no preview or '
                                                  'settings. It is a whole .MOL job and can still be sent.',
                                      foreground=COLOURS['warn'])).pack(anchor='w', fill='x', pady=(8, 0))
        self.fit_panel(body, left, right)
        self.schedule_redraw()

    def fill_mol_table(self, layers):
        """The .MOL's layers in run order, laid out like the colour table, as plain text."""
        t, bold = self.table, ('TkDefaultFont', 11, 'bold')
        ttk.Label(t, text='Run', font=bold).grid(row=0, column=0, padx=4, sticky='w')
        ttk.Label(t, text='Job', font=bold).grid(row=0, column=1, columnspan=3, padx=4, sticky='w')
        for c, h in enumerate(['Speed\nmm/s', 'Power\n%', 'Corner\n%', 'Line spacing\nmm']):
            ttk.Label(t, text=h, font=bold).grid(row=1, column=c, padx=(4, 12), sticky='sw')
        for i, e in enumerate(layers):
            r, top = 2 + 2 * i, (10, 2)
            first = ttk.Frame(t)
            first.grid(row=r, column=0, padx=4, pady=top, sticky='w')
            ttk.Label(first, text=str(i + 1)).pack(side='left')
            sw = tk.Canvas(first, width=26, height=18, highlightthickness=1, background=COLOURS['swatch'],
                           highlightbackground=COLOURS['swatch_edge'])
            if e['kind'] == 'engrave':
                sw.create_rectangle(2, 2, 26, 18, fill=e['colour'], outline='')
                job = 'Engrave, ' + ('both directions' if e['bidir'] else 'one direction')
            else:
                sw.create_rectangle(4, 4, 24, 16, outline=e['colour'], width=3)
                job = 'Cut'
            sw.pack(side='left', padx=(6, 0))
            ttk.Label(t, text=job).grid(row=r, column=1, columnspan=3, padx=4, pady=top, sticky='w')
            values = [mm_text(e['speed']), mm_text(e['power']),
                      mm_text(e['corner']) if e['kind'] == 'cut' else '–',
                      mm_text(e['spacing']) if e.get('spacing') else '–']
            for c, v in enumerate(values):
                ttk.Label(t, text=v).grid(row=r + 1, column=c, padx=4, sticky='w')

    def preview_area(self, body):
        """The left of the window: the preview and the start note under it. It takes all the width
        the settings panel on the right doesn't need (fit_panel), and grows with the window."""
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky='nsew')
        self.canvas = tk.Canvas(left, background=COLOURS['canvas'], highlightthickness=1,
                                highlightbackground=COLOURS['canvas_edge'],
                                highlightcolor=COLOURS['canvas_edge'],      # the same edge when it has the keys
                                width=380, height=380)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.schedule_redraw())
        self.start_note = ttk.Label(left, text=self.start_text(), foreground=COLOURS['hint'])
        self.start_note.pack(anchor='w', pady=(4, 0))
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.bind_zoom(self.canvas)
        return left

    def zoom_bar(self, parent):
        self.fit_button = ttk.Button(parent, text='Fit', width=4, command=self.zoom_fit)
        self.fit_button.pack(side='right')
        self.zoom_label = ttk.Label(parent, text='', foreground=COLOURS['hint'])
        self.zoom_label.pack(side='right', padx=(8, 6))
        self.update_zoom_bar()

    def build_ui(self):
        body = self.new_body()
        if not self.order:
            ttk.Label(body, text=svg2mol.nothing_found_message(self.skipped), wraplength=520).pack(pady=40)
            return
        left = self.preview_area(body)
        self.root.bind('<Escape>', lambda e: self.select(set()))
        selbar = ttk.Frame(left)
        selbar.pack(fill='x', pady=(6, 0))
        self.sel_label = ttk.Label(selbar, text='')
        self.sel_label.pack(side='left')
        self.undo_button = ttk.Button(selbar, text='Undo', command=self.undo)
        self.undo_button.pack(side='left', padx=(10, 4))
        ttk.Button(selbar, text='Save recoloured copy…', command=self.save_copy).pack(side='right')
        palette = ttk.Frame(left)
        palette.pack(fill='x', pady=(6, 0))
        ttk.Label(palette, text='Recolour:').pack(side='left', padx=(0, 6))
        for colour in PALETTE:
            box = tk.Canvas(palette, width=22, height=22, highlightthickness=1, cursor='hand2',
                            highlightbackground=COLOURS['swatch_edge'])
            box.create_rectangle(0, 0, 24, 24, fill=colour, outline='')
            box.bind('<Button-1>', lambda e, c=colour: self.recolour(c))
            box.pack(side='left', padx=2)
        self.recolour_menu = tk.Menu(palette, tearoff=False, postcommand=self.fill_recolour_menu)
        self.recolour_button = ttk.Menubutton(palette, text='Other', menu=self.recolour_menu)
        self.recolour_button.pack(side='left', padx=(8, 0))
        self.zoom_bar(palette)
        self.update_selection_bar()

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew', padx=(14, 0))
        self.save_area(right)

        # everything else scrolls when it doesn't fit (many colours, small window)
        self.wrapped = []               # labels that wrap to the panel's width
        panel = self.scroll_panel(right)
        for note in svg2mol.skipped_notes(self.skipped):
            self.wrap_label(ttk.Label(panel, text='⚠ ' + note[0].upper() + note[1:],
                                      foreground=COLOURS['warn'])).pack(anchor='w', fill='x')

        self.table = ttk.Frame(panel)
        self.table.pack(fill='x', pady=(4, 6))
        self.fill_table()

        self.wrap_label(ttk.Label(panel, text='Engraving runs first, then cuts (shapes inside other shapes first). '
                                              'Engraving needs ~36 mm free on each side at 500 mm/s.',
                                  foreground=COLOURS['hint'])).pack(anchor='w', fill='x', pady=(4, 8))
        self.fit_panel(body, left, right)
        self.schedule_estimate()

    def fit_panel(self, body, left, right):
        """Make the settings panel exactly as wide as its table (with room for the scroll bar) or its
        buttons, whichever is wider, and the window never narrower than that panel plus what the
        controls under the preview need (when the screen allows it)."""
        self.root.update_idletasks()
        panel = max(self.table.winfo_reqwidth() + self._scroll_bar_width, right.winfo_reqwidth())
        body.columnconfigure(1, minsize=panel + 14)         # + the gap between preview and panel
        need = left.winfo_reqwidth() + panel + 14 + 28      # + the window's padding
        self.root.minsize(min(need, self.root.winfo_screenwidth() - 40), 560)

    def save_area(self, right, name=None):
        """Pinned at the bottom of the panel: file name, saving and sending, and the estimate.
        `name`: the file name to offer (an opened .MOL's own), else the remembered one."""
        o = self.memory['options']
        save = ttk.Frame(right)
        save.pack(fill='x', side='bottom', pady=(6, 0))
        ttk.Label(save, text='File name').grid(row=0, column=0, sticky='w')
        self.name = tk.StringVar(value=name or o.get('name', 'LASER'))
        ttk.Entry(save, textvariable=self.name, width=10).grid(row=0, column=1, padx=4, sticky='w')
        ttk.Label(save, text='.MOL').grid(row=0, column=2, sticky='w')
        save.columnconfigure(3, weight=1)
        buttons = ttk.Frame(save)
        buttons.grid(row=1, column=0, columnspan=4, sticky='e', pady=(8, 0))
        self.laser_button = ttk.Button(buttons, text='Send to laser', command=self.send_laser)
        self.laser_button.pack(side='left', padx=4)
        self.usb_button = ttk.Button(buttons, text='Save to USB stick', command=self.save_usb)
        self.usb_button.pack(side='left', padx=4)
        ttk.Button(buttons, text='Save as file…', command=self.save_file).pack(side='left', padx=(4, 0))
        self.eject = tk.BooleanVar(value=bool(o.get('eject', True)))
        ttk.Checkbutton(right, text='Eject the USB stick after saving', variable=self.eject).pack(
            side='bottom', anchor='e', pady=(10, 0))
        # the estimate (or what's wrong with a number) stays in view however many colours there are
        self.estimate = ttk.Label(right, text='', wraplength=300)
        self.estimate.pack(side='bottom', anchor='w', fill='x', pady=(6, 0))
        right.bind('<Configure>', lambda e: self.estimate.configure(wraplength=max(e.width - 8, 100)), add='+')

    def scroll_panel(self, parent):
        """A frame inside a canvas that scrolls (vertically, and sideways only if it can't be fitted)
        when it is bigger than `parent`'s space; the frame is as wide as the space."""
        area = ttk.Frame(parent)
        area.pack(fill='both', expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        view = tk.Canvas(area, highlightthickness=0, borderwidth=0, background=COLOURS['bg'],
                         yscrollincrement=1, width=1)      # as wide as fit_panel makes the panel
        view.grid(row=0, column=0, sticky='nsew')
        ybar = ttk.Scrollbar(area, orient='vertical', command=view.yview)
        xbar = ttk.Scrollbar(area, orient='horizontal', command=view.xview)
        view.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self._scroll_bar_width = ybar.winfo_reqwidth()
        panel = ttk.Frame(view)
        item = view.create_window(0, 0, window=panel, anchor='nw')

        def fit(e=None):
            vw, vh = view.winfo_width(), view.winfo_height()
            if vw <= 1:
                return
            for label in self.wrapped:
                label.configure(wraplength=max(vw - 8, 100))
            pw, ph = max(vw, self.table.winfo_reqwidth()), panel.winfo_reqheight()
            view.itemconfigure(item, width=pw)
            view.configure(scrollregion=(0, 0, pw, ph))
            # scroll bars only when needed
            for bar, show, place in ((ybar, ph > vh, dict(row=0, column=1, sticky='ns')),
                                     (xbar, pw > vw, dict(row=1, column=0, sticky='ew'))):
                if show and not bar.winfo_manager():
                    bar.grid(**place)
                elif not show and bar.winfo_manager():
                    bar.grid_remove()
            if ph <= vh:
                view.yview_moveto(0)
            if pw <= vw:
                view.xview_moveto(0)
        view.bind('<Configure>', fit)
        panel.bind('<Configure>', fit)

        self._scroll = view, ybar
        if not getattr(self, '_wheel_bound', False):
            for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                self.root.bind_all(seq, self.wheel, add='+')
            with contextlib.suppress(tk.TclError):          # no TouchpadScroll before Tk 9
                self.root.bind_all('<TouchpadScroll>', lambda e: self.wheel(e, touchpad=True), add='+')
            self._wheel_bound = True
        return panel

    def wheel(self, e, touchpad=False):
        """Mouse wheel / two-finger scroll over the settings panel scrolls it (not over a spin box,
        where the wheel changes the number as before)."""
        view, ybar = getattr(self, '_scroll', (None, None))
        w = e.widget
        if view is None or not view.winfo_exists() or not ybar.winfo_manager() \
                or not isinstance(w, tk.Misc) or not (str(w) + '.').startswith(str(view) + '.') \
                or w.winfo_class() in ('TSpinbox', 'Spinbox'):
            return
        if e.num in (4, 5):                         # X11 wheel
            px = -30 if e.num == 4 else 30
        elif touchpad:                              # Tk 9 two-finger scroll
            px = -int(self.root.tk.splitlist(self.root.tk.call('tk::PreciseScrollDeltas', e.delta))[1])
        elif abs(e.delta) >= 120:                   # Tk 9 everywhere, Windows: 120 a notch
            px = -e.delta // 4
        else:                                       # Tk 8.6 on macOS
            px = -e.delta * 10
        view.yview_scroll(px, 'units')              # yscrollincrement is 1 px

    def wrap_label(self, label):
        self.wrapped.append(label)
        return label

    def fill_table(self):
        """(Re)build the colour rows. Only this part changes when shapes are recoloured, so the
        preview and the colour boxes stay put (rebuilding the box being clicked confused macOS)."""
        for w in self.table.winfo_children():
            w.destroy()
        self.rows = {}
        # One line per colour, across the settings panel's width (the hex code shows on the swatch):
        #   Colour | Job | Speed | Power | Corner power | Line spacing | Shapes | Run
        t = self.table
        first = 2                                       # column of the first number box
        for c in range(first, first + len(COLUMNS)):
            t.columnconfigure(c, uniform='numbers', weight=1)   # boxes of one width share the room
        heads = [('', ''), ('Job', '')] + [COLUMN_NAMES[f] for f in COLUMNS] + [('Shapes', ''), ('Run', '')]
        for c, (name, unit) in enumerate(heads):
            if not name:                                # the swatches need no heading
                continue
            head = ttk.Frame(t)
            # names apart from the next one; Shapes stays flush with its right-aligned counts
            head.grid(row=0, column=c, padx=2 if name in ('Shapes', 'Run') else (2, 8), sticky='sew')
            side = 'e' if name == 'Shapes' else 'w'
            ttk.Label(head, text=name, font=TABLE_HEAD_FONT).pack(anchor=side)
            if unit:
                ttk.Label(head, text=unit, font='TkSmallCaptionFont', foreground=COLOURS['hint']).pack(anchor=side)
        if not hasattr(self, '_number_check'):
            # digits and one decimal point only; empty while typing (collect says what's missing)
            self._number_check = self.root.register(lambda v: re.fullmatch(r'\d*\.?\d*', v) is not None)
        for i, key in enumerate(self.order):
            self._add_row(t, 1 + i, key, first)

    def _add_row(self, table, r, key, first):
        """One colour on table row r: swatch, job, the numbers from column `first`, shapes, run."""
        kind, colour = key
        saved = self.memory[kind].get(colour, {})
        row = {'use': tk.BooleanVar(value=saved.get('use', True)), 'kind': kind}
        pad = dict(padx=2, pady=3)
        sw = tk.Canvas(table, width=26, height=18, highlightthickness=1, background=COLOURS['swatch'],
                       highlightbackground=COLOURS['swatch_edge'])
        if kind == 'engrave':
            sw.create_rectangle(2, 2, 26, 18, fill=colour, outline='')
        else:
            sw.create_rectangle(4, 4, 24, 16, outline=colour, width=3)
        sw.grid(row=r, column=0, sticky='w', **pad)
        hover_tip(sw, colour)
        if kind == 'engrave':
            count = len(self.fills[colour])
            row['mode'] = saved.get('mode', 'engrave')
            jobs, can_switch = FILL_JOBS, True
        else:
            count = len(self.cuts[colour])
            can_switch = any(closed(p) for p in self.cuts[colour])     # open lines can't be filled
            row['mode'] = saved.get('mode', 'cut') if can_switch else 'cut'
            jobs = JOBS
        # a native pop-up menu (not a combobox: nothing to select as text, opens on the first click)
        job = tk.StringVar()
        current = next(k for k, v in jobs.items() if v == row['mode'])
        box = ttk.OptionMenu(table, job, current, *jobs, style='Job.TMenubutton',
                             command=lambda label, k=key, j=jobs: self.switch_mode(k, j[label]))
        # as narrow as the longest job it can show allows (only its own when it can't be switched;
        # width counts '0's); the column takes the widest row's
        font = tkfont.nametofont('TkDefaultFont')
        shown = jobs if can_switch else [current]
        box.configure(width=-(-max(map(font.measure, shown)) // font.measure('0')))
        if not can_switch:
            box.state(['disabled'])
        box.grid(row=r, column=1, sticky='ew', **pad)
        for c, field in enumerate(COLUMNS):
            var = tk.StringVar()
            var.trace_add('write', lambda *_: self.schedule_estimate())
            lo, hi = LIMITS[field]
            spin = ttk.Spinbox(table, from_=lo, to=hi, increment=0.01 if field == 'interval' else 1,
                               width=4, textvariable=var, validate='key',
                               validatecommand=(self._number_check, '%P'))
            spin.grid(row=r, column=first + c, sticky='ew', **pad)
            row[field], row[field + '_box'] = var, spin
        last = first + len(COLUMNS)
        ttk.Label(table, text=str(count)).grid(row=r, column=last, sticky='e', **pad)
        ttk.Checkbutton(table, variable=row['use'], command=self.schedule_estimate).grid(
            row=r, column=last + 1, sticky='w', **pad)
        self.rows[key] = row
        self.fill_fields(key, saved)

    def mode_settings(self, saved, mode, kind):
        """Remembered speed/power (and line spacing when engraving) for a row in the given mode.
        A colour without its own line spacing gets the old single setting, if one was saved."""
        new = dict(NEW_COLOUR[mode])
        if mode == 'engrave':
            new['interval'] = self.memory['options'].get('interval', INTERVAL)
        if kind == 'cut' and mode == 'engrave':
            return {**new, **saved.get('as_engrave', {})}
        if kind == 'engrave' and mode == 'cut':
            return {**new, **saved.get('as_cut', {})}
        return {**new, **{k: v for k, v in saved.items() if k in FIELDS[mode]}}

    def fill_fields(self, key, saved):
        row = self.rows[key]
        s = self.mode_settings(saved, row['mode'], key[0])
        for field in COLUMNS:
            if field in FIELDS[row['mode']]:
                row[field].set(f'{s[field]:g}')
                row[field + '_box'].state(['!disabled'])
            else:
                row[field].set('')
                row[field + '_box'].state(['disabled'])

    def switch_mode(self, key, mode):
        row = self.rows[key]
        if mode == row['mode']:
            return
        self.remember_row(key)          # keep what was typed for the old mode
        row['mode'] = mode
        self.fill_fields(key, self.memory[key[0]].get(key[1], {}))
        self.schedule_estimate()

    # ------------------------------------------------------------ settings

    def collect(self):
        """Return (settings, order, cuts, fills) for ticked rows; raises ValueError for bad numbers."""
        settings, engr, cut = {}, [], []
        cuts, fills = {}, {}
        for key in self.order:
            row = self.rows[key]
            if not row['use'].get():
                continue
            mode = row['mode']
            s = {}
            for field in FIELDS[mode]:
                try:
                    v = float(row[field].get())
                except ValueError:
                    raise ValueError(f'{key[1]}: "{row[field].get()}" is not a number '
                                     f'({FIELD_NAME.get(field, field)}).')
                lo, hi = LIMITS[field]
                if not lo <= v <= hi:
                    raise ValueError(f'{key[1]}: {FIELD_NAME.get(field, field)} must be between {lo:g} and {hi:g}.')
                s[field] = v
            if mode == 'engrave':
                s['bidir'] = 1.0 if self.bidir.get() else 0.0
            colour = key[1]
            if key[0] == 'engrave' and mode == 'cut':
                colour += EDGE
                cuts[colour] = [ring for rings, _ in self.fills[key[1]] for ring in rings]
            elif key[0] == 'engrave':
                fills[colour] = self.fills[colour]
            elif mode == 'engrave':
                colour += OUTLINE
                rings = [p for p in self.cuts[key[1]] if closed(p)]
                fills[colour] = [(rings, 'evenodd')]
            else:
                cuts[colour] = self.cuts[colour]
            (engr if mode == 'engrave' else cut).append((mode, colour))
            settings[(mode, colour)] = s
        if not settings:
            raise ValueError('Tick at least one colour.')
        # engraving first; cut colours inside other colours' shapes before them, else in table order
        return settings, svg2mol.run_order(cuts, fills, engr + cut), cuts, fills

    def remember_row(self, key):
        row = self.rows[key]
        kind, colour = key
        entry = self.memory[kind].setdefault(colour, {})
        entry['use'] = row['use'].get()
        entry['mode'] = row['mode']
        target = entry
        if kind == 'cut' and row['mode'] == 'engrave':
            target = entry.setdefault('as_engrave', {})
        elif kind == 'engrave' and row['mode'] == 'cut':
            target = entry.setdefault('as_cut', {})
        for field in FIELDS[row['mode']]:
            try:
                target[field] = float(row[field].get())
            except ValueError:
                pass

    def remember(self):
        for key in self.rows:
            self.remember_row(key)
        o = self.memory['options']
        o.update(bidir=self.bidir.get(), origin=self.origin.get(), whole=self.whole.get(), eject=self.eject.get())
        if self.mol is None:            # a .MOL's own name isn't kept for drawings
            o['name'] = self.name.get().strip().upper()
        save_memory(self.memory)

    def build(self):
        settings, order, cuts, fills = self.collect()
        job, report = svg2mol.build_job(cuts, fills, settings, order, origin=self.origin.get(),
                                        design=self.design())
        return job, report, cuts, fills

    def design(self):
        """The whole drawing for build_job's `design` if unticked colours keep their place, else None."""
        return (self.cuts, self.fills) if self.whole.get() else None

    @contextlib.contextmanager
    def busy(self, always=True):
        """The system's busy cursor over the window while it works and can't answer clicks
        (`always=False`: not for small drawings, where the work is too quick to see it)."""
        if self._busy or not (always or self._points >= BUSY_POINTS):
            yield
            return
        self._busy = True
        # On macOS (and Windows) Tk only applies a cursor change to the widget it last saw the
        # pointer in; anywhere else it waits for the next mouse event, which can't come while the
        # window works. So the widget under the pointer gets it too ('' children of the window
        # inherit). Not `tk busy`: mapping its cover window repaints the whole window each time.
        root = self.root
        cursor = 'watch' if root.tk.call('tk', 'windowingsystem') == 'x11' else 'wait'
        try:
            under = root.winfo_containing(*root.winfo_pointerxy())
        except (KeyError, tk.TclError):
            under = None
        changed = []
        for w in [root] + ([under] if under not in (None, root) else []):
            try:
                changed.append((w, str(w.cget('cursor'))))
                w.configure(cursor=cursor)
            except tk.TclError:
                pass
        root.update_idletasks()
        root.winfo_pointerxy()          # X11: a round trip, so the change reaches the screen now
        try:
            yield
        finally:
            self._busy = False
            for w, old in changed:
                try:
                    w.configure(cursor=old)
                except tk.TclError:         # rebuilt meanwhile (the colour table)
                    pass

    def count_points(self):
        self._points = (sum(len(p) for v in self.cuts.values() for p in v)
                        + sum(len(r) for v in self.fills.values() for rings, _ in v for r in rings))

    # ------------------------------------------------------------ preview + estimate

    def schedule_estimate(self):
        self._preview = None
        if self._estimate_job:
            self.root.after_cancel(self._estimate_job)
        self._estimate_job = self.root.after(400, self.update_estimate)

    def schedule_redraw(self):
        """The canvas changed size: the job is the same, so only the preview is drawn again."""
        if self._redraw_job:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(400, self.redraw)

    @busy_cursor(always=False)
    def redraw(self):
        self._redraw_job = None
        if self._preview is not None:
            self.draw_preview(*self._preview)
        elif self.mol is not None:
            self.draw_unreadable()
        elif not self._estimate_job:
            self.update_estimate()

    def draw_unreadable(self):
        c = self.canvas
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        c.create_text(w / 2, h / 2, width=max(w - 60, 100), justify='center', fill=COLOURS['hint'],
                      text='This .MOL file can\'t be previewed.\nIt can still be sent to the laser as it is.')

    @busy_cursor(always=False)
    def update_estimate(self):
        self._estimate_job = None
        if not getattr(self, 'rows', None):
            return
        try:
            settings, order, cuts, fills = self.collect()
            scan = {}
            job, report = svg2mol.build_job(cuts, fills, settings, order, scan=scan, origin=self.origin.get(),
                                            design=self.design())
        except (ValueError, svg2mol.ConversionError) as e:
            self.estimate.configure(text=f'⚠ {e}', foreground=COLOURS['error'])
            # no job: the start shown on the whole drawing
            self._preview = ({}, {}, {}, None, svg2mol.origin_point(self.cuts, self.fills, self.origin.get()))
            self.draw_preview(*self._preview)
            return
        secs = job.estimate_seconds()
        self.size_label.configure(text=report[0])
        self.estimate.configure(text=f'Estimated time: {secs // 60} min {secs % 60:02d} s', foreground='')
        self._preview = (cuts, fills, {colour: s['interval'] for (mode, colour), s in settings.items()
                                       if mode == 'engrave'}, scan,
                         svg2mol.origin_point(*(self.design() or (cuts, fills)), self.origin.get()))
        self.draw_preview(*self._preview)

    def draw_preview(self, cuts, fills, spacing, scan=None, start=None):
        """Draw the whole drawing faintly, and what will run in its colours: engraving as the job's
        own scan lines at each colour's line spacing (`spacing`, mm per engrave layer; `scan`, the
        lines build_job made, else they are worked out here), and the red start dot at `start`
        (design mm, from svg2mol.origin_point)."""
        c = self.canvas
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        box = self.drawing_box()
        if box is None or w < 20:
            return
        x0, y0, x1, y1 = box
        pad = 20
        self._fit = (x0, y0, x1, y1, min((w - 2 * pad) / max(x1 - x0, 1e-3), (h - 2 * pad) / max(y1 - y0, 1e-3)))
        self.view = self.zoomed_view()
        ox, oy, k, _ = self.view
        seen = self.seen_box()

        def xy(p):
            return pad + (p[0] - ox) * k, pad + (p[1] - oy) * k

        def flat(poly):                 # xy() of every point, as one list
            return [q for x, y in poly for q in (pad + (x - ox) * k, pad + (y - oy) * k)]

        def shown(obj, outlines):       # zoomed in: only shapes in or near the view are drawn
            if seen is None:
                return True
            box = self.shape_box(obj, outlines)[0]
            return box is not None and box[0] <= seen[2] and box[2] >= seen[0] \
                and box[1] <= seen[3] and box[3] >= seen[1]
        for v in self.fills.values():
            for entry in v:
                if shown(entry, entry[0]):
                    for r in entry[0]:
                        self.line(flat(r), fill=COLOURS['faint'])
        for v in self.cuts.values():
            for p in v:
                if shown(p, [p]):
                    self.line(flat(p), fill=COLOURS['faint'])
        for colour, shapes in fills.items():
            # the same lines svg2mol burns; where they are under a pixel apart on screen they look
            # solid anyway, so only every n-th is drawn (at most ~2 per pixel row keeps big fills fast)
            lines = scan[colour] if scan and colour in scan else svg2mol.scanlines(shapes, spacing[colour])[0]
            every = max(1, int(1 / (spacing[colour] * k)))
            shade = self.shade(colour)
            for y, spans in lines[::every]:
                if seen is not None and not seen[1] <= y <= seen[3]:
                    continue
                for a, b in spans:
                    if seen is None or (a <= seen[2] and b >= seen[0]):
                        self.line((*xy((a, y)), *xy((b, y))), fill=shade)
        for colour, paths in cuts.items():
            for p in paths:
                if shown(p, [p]):
                    self.line(flat(p), fill=self.shade(colour), width=1.5)
        if start is not None:
            sx, sy = xy(start)
            c.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, fill=COLOURS['start'], outline='', tags='start')
        self.draw_selection()
        self.update_zoom_bar()

    def drawing_box(self):
        """(x0, y0, x1, y1) mm of everything in the preview, None when there is nothing."""
        if self.mol is not None:
            return self.mol.get('box')
        boxes = [b for b in (self.shape_box(obj, outlines)[0] for obj, outlines in self.shapes()) if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def shade(self, colour):
        """How a layer shows in the preview (an opened .MOL's layers are keyed by their run number)."""
        if self.mol is not None:
            colour = next(e['colour'] for e in self.mol['layers'] if e['key'] == colour)
        return screen_ink(ink(colour))

    # ------------------------------------------------------------ settings

    def start_text(self):
        if self.mol is not None:
            origin = self.mol.get('origin')
            if not self.mol['layers']:
                return 'Start position: as saved in the file.'
            if origin is None:
                return 'Red dot = start: put the laser head there (as saved in the file).'
            words = ORIGIN_WORDS.get(origin, origin)
            return f'Red dot = start: put the laser head there ({words} of the design, as saved in the file).'
        return (f'Red dot = start: put the laser head there '
                f'({ORIGIN_WORDS[self.origin.get()]} of the design).')

    def show_settings(self):
        """The Settings window: start position and engraving direction, for every job. Changes
        apply at once and are remembered. With a .MOL open it shows the file's own, read-only."""
        if self.settings_window is not None:
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        top = tk.Toplevel(self.root, background=COLOURS['bg'])
        self.settings_window = top
        top.transient(self.root)
        top.resizable(False, False)
        top.bind('<Escape>', lambda e: self.close_settings())
        top.protocol('WM_DELETE_WINDOW', self.close_settings)
        self.fill_settings()
        top.update_idletasks()
        # under the Settings button, at the right of the main window
        x = self.root.winfo_rootx() + self.root.winfo_width() - top.winfo_reqwidth() - 14
        y = self.root.winfo_rooty() + 50
        top.geometry(f'+{max(x, 0)}+{max(y, 0)}')
        top.focus_force()

    def fill_settings(self):
        """(Re)build the Settings window for the open file: the user's settings for a drawing, or
        a .MOL's own settings, every control disabled (nothing is changed or saved)."""
        top, mol = self.settings_window, self.mol
        for w in top.winfo_children():
            w.destroy()
        box = ttk.Frame(top, padding=16)
        box.pack(fill='both', expand=True)
        close = ttk.Button(box, text='Close', command=self.close_settings)
        if mol is None:
            top.title('Settings')
            origin, bidir, command = self.origin, self.bidir, self.settings_changed
        else:
            name = self.file_label.cget('text')
            top.title(f'Settings in {name} (read-only)')
            ttk.Label(box, text=f'Stored in {name}: shown for information, can\'t be changed here.',
                      foreground=COLOURS['warn'], wraplength=300, justify='left').pack(anchor='w', pady=(0, 12))
            if not mol['layers']:
                ttk.Label(box, text='Laser Prepare can\'t read the settings from this file. '
                                    'It is sent exactly as it is.',
                          wraplength=300, justify='left').pack(anchor='w')
                close.pack(anchor='e', pady=(14, 0))
                return
            # separate variables, so nothing here reaches the user's own settings
            origin = tk.StringVar(top, value=mol['origin'] or 'none')
            engraves = [(i + 1, e['bidir']) for i, e in enumerate(mol['layers']) if e['kind'] == 'engrave']
            bidir = tk.BooleanVar(top, value=bool(engraves) and all(b for _, b in engraves))
            command = None

        where = ttk.LabelFrame(box, text='Start position', padding=10)
        where.pack(fill='x')
        ttk.Label(where, text='Where the laser head is when the job starts:',
                  foreground=COLOURS['hint']).pack(anchor='w', pady=(0, 8))
        # the design as a box, with a choice at each corner and in the middle
        design = ttk.Frame(where, style='Card.TFrame', padding=8)
        design.pack(anchor='w')
        spots = {'top-left': (0, 0, 'w'), 'top-right': (0, 2, 'e'), 'center': (1, 1, ''),
                 'bottom-left': (2, 0, 'w'), 'bottom-right': (2, 2, 'e')}
        for spot, (r, col, sticky) in spots.items():
            words = ORIGIN_WORDS[spot]
            button = ttk.Radiobutton(design, text=words[0].upper() + words[1:], value=spot,
                                     variable=origin, command=command)
            button.grid(row=r, column=col, sticky=sticky, padx=4, pady=8)
            if mol is not None:
                button.state(['disabled'])
        design.columnconfigure(1, minsize=90)
        if mol is not None and origin.get() not in spots:
            where_text = ('This job doesn\'t start at a corner or the centre of the design: '
                          'LaserCut placed it on the bed.')
            ttk.Label(where, text=where_text, wraplength=300, justify='left').pack(anchor='w', pady=(8, 0))
        if mol is None:                 # a .MOL doesn't say which shapes it left out
            ttk.Checkbutton(where, text='Outline the whole drawing,\nunticked colours too',
                            variable=self.whole, command=command).pack(anchor='w', pady=(8, 0))

        eng = ttk.LabelFrame(box, text='Engraving', padding=10)
        eng.pack(fill='x', pady=(12, 0))
        if mol is not None and not engraves:
            ttk.Label(eng, text='No engraving in this file.').pack(anchor='w')
        else:
            check = ttk.Checkbutton(eng, text='Burn in both directions', variable=bidir, command=command)
            check.pack(anchor='w')
            if mol is not None:
                check.state(['disabled'])
                if len({b for _, b in engraves}) > 1:
                    check.state(['alternate'])
                    both = ', '.join(str(i) for i, b in engraves if b)
                    one = ', '.join(str(i) for i, b in engraves if not b)
                    ttk.Label(eng, text=f'Layers differ: {both} both directions; {one} one direction.',
                              wraplength=300, justify='left').pack(anchor='w', pady=(6, 0))

        close.pack(anchor='e', pady=(14, 0))

    def close_settings(self):
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None

    def settings_changed(self):
        if self.mol is not None:        # a .MOL's settings are only shown, never changed or saved
            return
        o = self.memory['options']
        o.update(bidir=self.bidir.get(), origin=self.origin.get(), whole=self.whole.get())
        try:
            save_memory(self.memory)
        except OSError:
            pass                        # remembered again when the job is saved or the window closes
        if getattr(self, 'rows', None):
            self.start_note.configure(text=self.start_text())
            self.schedule_estimate()

    # ------------------------------------------------------------ zooming + panning

    def zoomed_view(self):
        """(x, y in mm at the canvas point (pad, pad), px per mm, pad) for the current zoom: the
        whole drawing, or zoomed in and kept inside what the whole-drawing view shows."""
        x0, y0, x1, y1, fit = self._fit
        pad = 20
        if self._zoom is None or self._zoom[0] <= fit * 1.001:
            self._zoom = None
            return x0, y0, fit, pad
        k, cx, cy = self._zoom
        k = min(k, self.zoom_max())
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        ox = min(max(cx - (w / 2 - pad) / k, x0), x0 + (w - 2 * pad) * (1 / fit - 1 / k))
        oy = min(max(cy - (h / 2 - pad) / k, y0), y0 + (h - 2 * pad) * (1 / fit - 1 / k))
        self._zoom = (k, ox + (w / 2 - pad) / k, oy + (h / 2 - pad) / k)
        return ox, oy, k, pad

    def zoom_max(self):
        fit = self._fit[4]
        return max(min(ZOOM_MAX * fit, ZOOM_PX_PER_MM), 4 * fit)

    def seen_box(self):
        """Zoomed in: the part of the drawing (mm) that is drawn, the view and half a view around
        it (so a little panning shows what is drawn already); None for the whole drawing."""
        if self._zoom is None:
            return None
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        (ax, ay), (bx, by) = self.to_drawing(0, 0), self.to_drawing(w, h)
        mx, my = (bx - ax) / 2, (by - ay) / 2
        return ax - mx, ay - my, bx + mx, by + my

    def bind_zoom(self, c):
        aqua = self.root.tk.call('tk', 'windowingsystem') == 'aqua'
        self._zoom_mods = 0x4 | (0x8 if aqua else 0)      # Control, and Command on macOS
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            c.bind(seq, self.canvas_wheel)
        for seq in ('<Button-6>', '<Button-7>'):            # sideways wheel on X11; Tk 8.6 has no
            with contextlib.suppress(tk.TclError):          # buttons above 5 ("bad button number")
                c.bind(seq, self.canvas_wheel)
        with contextlib.suppress(tk.TclError):              # no TouchpadScroll before Tk 9
            c.bind('<TouchpadScroll>', lambda e: self.canvas_wheel(e, touchpad=True))
        # the middle button: Button-2, but Button-3 in Tk 8.6 on macOS (where 2 is the right button)
        middle = 3 if aqua and tk_version(self.root) < (8, 7) else 2
        c.bind(f'<ButtonPress-{middle}>', self.pan_start)
        c.bind(f'<B{middle}-Motion>', self.pan_move)
        c.bind(f'<ButtonRelease-{middle}>', self.pan_end)
        for key in ('<plus>', '<equal>', '<KP_Add>'):
            c.bind(key, lambda e: self.zoom_key(ZOOM_STEP))
        for key in ('<minus>', '<KP_Subtract>'):
            c.bind(key, lambda e: self.zoom_key(1 / ZOOM_STEP))
        for key in ('<Key-0>', '<KP_0>', '<KP_Insert>'):
            c.bind(key, lambda e: self.zoom_fit())
        c.bind('<KeyPress-space>', lambda e: setattr(self, '_space', True))
        c.bind('<KeyRelease-space>', lambda e: setattr(self, '_space', False))
        c.bind('<FocusOut>', lambda e: setattr(self, '_space', False))

    def canvas_wheel(self, e, touchpad=False):
        """Wheel / two-finger scroll over the preview pans it when zoomed in (Shift: sideways);
        with Control (or Command on macOS) it zooms in and out around the pointer."""
        if not getattr(self, 'view', None):
            return 'break'
        dx = dy = 0.0                               # how far the drawing moves, in pixels
        if e.num in (4, 5):                         # X11 wheel (Tk 8.6)
            dy = 40.0 if e.num == 4 else -40.0
        elif e.num in (6, 7):                       # X11 sideways wheel
            dx = 40.0 if e.num == 6 else -40.0
        elif touchpad:                              # Tk 9 two-finger scroll
            dx, dy = (float(v) for v in self.root.tk.splitlist(
                self.root.tk.call('tk::PreciseScrollDeltas', e.delta)))
        elif abs(e.delta) >= 120:                   # Tk 9 everywhere, Windows: 120 a notch
            dy = e.delta / 3
        else:                                       # Tk 8.6 on macOS
            dy = e.delta * 10.0
        if e.state & 0x1 and not touchpad and not dx:
            dx, dy = dy, 0.0
        if e.state & self._zoom_mods:
            self.zoom_at(e.x, e.y, 1.0025 ** (dy or dx))
        else:
            self.pan_by(dx, dy)
        return 'break'

    def zoom_key(self, factor):
        c = self.canvas
        self.zoom_at(c.winfo_width() / 2, c.winfo_height() / 2, factor)

    def zoom_fit(self):
        if self._zoom is not None:
            self._zoom = None
            self.change_view()

    def zoom_at(self, px, py, factor):
        """Zoom by `factor` keeping the drawing point under (px, py) where it is."""
        if not getattr(self, 'view', None):
            return
        ox, oy, k, pad = self.view
        fit = self._fit[4]
        new = min(max(k * factor, fit), self.zoom_max())
        if abs(new - k) < 1e-9 * k:
            return
        mx, my = self.to_drawing(px, py)
        c = self.canvas
        ox, oy = mx - (px - pad) / new, my - (py - pad) / new
        self._zoom = (new, ox + (c.winfo_width() / 2 - pad) / new, oy + (c.winfo_height() / 2 - pad) / new)
        self.change_view()

    def pan_by(self, dx, dy):
        """Move the drawing by (dx, dy) pixels (only when zoomed in: the whole drawing stays put)."""
        if self._zoom is None or not (dx or dy):
            return
        k, cx, cy = self._zoom
        self._zoom = (k, cx - dx / k, cy - dy / k)
        self.change_view()

    def pan_start(self, e):
        self.canvas.focus_set()
        self._pan = (e.x, e.y)
        if self._zoom is not None:
            self.canvas.configure(cursor='fleur')

    def pan_move(self, e):
        if self._pan is None:
            return
        x, y = self._pan
        self._pan = (e.x, e.y)
        self.pan_by(e.x - x, e.y - y)

    def pan_end(self, e=None):
        self._pan = None
        self.canvas.configure(cursor='')

    def change_view(self):
        """The view changed: move and scale what is drawn now, so the preview follows the pointer at
        once, and draw it afresh (scan lines for the new scale, shapes come into view) once it rests."""
        old = self.view
        self.view = new = self.zoomed_view()
        f = new[2] / old[2]
        c = self.canvas
        pad = new[3]
        c.scale('all', pad, pad, f, f)
        c.move('all', (old[0] - new[0]) * new[2], (old[1] - new[1]) * new[2])
        self.update_zoom_bar()
        if self._zoom_job:
            self.root.after_cancel(self._zoom_job)
        self._zoom_job = self.root.after(ZOOM_REDRAW_MS, self.zoom_redraw)

    def zoom_redraw(self):
        self._zoom_job = None
        if self._preview is not None:
            self.draw_preview(*self._preview)

    def update_zoom_bar(self):
        if not getattr(self, 'zoom_label', None) or not self.zoom_label.winfo_exists():
            return
        if self._zoom is None or not getattr(self, '_fit', None):
            mod = 'Cmd' if self._zoom_mods & 0x8 else 'Ctrl'
            self.zoom_label.configure(text=f'{mod} + scroll to zoom')
            self.fit_button.state(['disabled'])
        else:
            self.zoom_label.configure(text=f'{self._zoom[0] / self._fit[4] * 100:.0f} %')
            self.fit_button.state(['!disabled'])

    # ------------------------------------------------------------ selecting + recolouring

    def shapes(self):
        """Every shape as (object, outlines): a cut polyline, or a fill shape with its rings."""
        for v in self.cuts.values():
            for p in v:
                yield p, [p]
        for v in self.fills.values():
            for entry in v:
                yield entry, entry[0]

    def shape_box(self, obj, outlines):
        """(box of all its points, box of its closed rings) as (x0, y0, x1, y1), None when there are
        none; worked out once per shape."""
        got = self._boxes.get(id(obj))
        if got is None or got[0] is not obj:        # the shape is kept with its boxes, so ids stay unique
            def box(polys):
                xs = [x for o in polys for x, _ in o]
                ys = [y for o in polys for _, y in o]
                return (min(xs), min(ys), max(xs), max(ys)) if xs else None
            got = self._boxes[id(obj)] = (obj, box(outlines), box([o for o in outlines if closed(o)]))
        return got[1], got[2]

    def to_drawing(self, cx, cy):
        x0, y0, k, pad = self.view
        return (cx - pad) / k + x0, (cy - pad) / k + y0

    def to_screen(self, p):
        x0, y0, k, pad = self.view
        return pad + (p[0] - x0) * k, pad + (p[1] - y0) * k

    def hit(self, pt):
        """The shapes under a click: the nearest line within a few pixels, else the smallest shape
        the point is inside. Shapes that tie (an outline that is both cut and engraved) all count."""
        tol = CLICK_PX / self.view[2]
        x, y = pt
        m = tol * 1.000001              # boxes only rule out shapes clearly further away than tol
        near = []
        for obj, outlines in self.shapes():
            box, _ = self.shape_box(obj, outlines)
            if box is None or not (box[0] - m <= x <= box[2] + m and box[1] - m <= y <= box[3] + m):
                continue
            d = min((segment_distance(pt, a, b) for o in outlines for a, b in zip(o, o[1:])), default=tol + 1)
            if d <= tol:
                near.append((d, id(obj)))
        if near:
            best = min(near)[0]
            return {i for d, i in near if d - best < 1e-6}
        inside = []
        for obj, outlines in self.shapes():
            _, box = self.shape_box(obj, outlines)
            if box is None or not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            rings = [o for o in outlines if closed(o)]
            if sum(svg2mol.inside(pt, r) for r in rings) % 2:
                inside.append(((box[2] - box[0]) * (box[3] - box[1]), id(obj)))
        if not inside:
            return set()
        best = min(inside)[0]
        return {i for a, i in inside if a <= best * 1.001}

    def press(self, e):
        self.canvas.focus_set()                 # for the zoom keys and Space
        self.canvas.delete('band')
        if self._space:                         # Space + drag moves the view
            self._press = None
            self.pan_start(e)
            return
        self._press = (e.x, e.y)

    def drag(self, e):
        if self._pan is not None:
            self.pan_move(e)
            return
        if getattr(self, '_press', None) is None or self.mol is not None:
            return
        x, y = self._press
        self.canvas.delete('band')
        self.canvas.create_rectangle(x, y, e.x, e.y, outline=SELECTED, dash=(3, 3), tags='band')

    def release(self, e):
        if self._pan is not None:
            self.pan_end()
            return
        if self.mol is not None:            # nothing to select in a .MOL
            return
        if not getattr(self, 'view', None) or getattr(self, '_press', None) is None:
            return
        self.canvas.delete('band')
        (x, y), add = self._press, bool(e.state & 0x1)       # Shift adds to the selection
        if abs(e.x - x) <= DRAG_PX and abs(e.y - y) <= DRAG_PX:
            picked = self.hit(self.to_drawing(e.x, e.y))
            if add:
                picked = self.selection ^ picked
        else:
            ax, ay = self.to_drawing(min(x, e.x), min(y, e.y))
            bx, by = self.to_drawing(max(x, e.x), max(y, e.y))
            picked = set()
            for obj, outlines in self.shapes():
                box, _ = self.shape_box(obj, outlines)
                if box is None or (ax <= box[0] and box[2] <= bx and ay <= box[1] and box[3] <= by):
                    picked.add(id(obj))
            if add:
                picked |= self.selection
        self.select(picked)

    def select(self, ids):
        self.selection = set(ids)
        self._note = None
        self.draw_selection()
        self.update_selection_bar()

    def draw_selection(self):
        """Selected shapes: a light-blue glow behind them, and the shape on top in its own colour,
        so a recolour shows straight away."""
        if not getattr(self, 'canvas', None) or not getattr(self, 'view', None):
            return
        self.canvas.delete('sel')
        if not self.selection:
            return
        x0, y0, k, pad = self.view
        sel, lines = self.selection, []
        seen = self.seen_box()                      # zoomed in: only what is in or near the view
        for d in (self.cuts, self.fills):           # the order of shapes()
            for colour, v in d.items():
                for obj in v:
                    if id(obj) in sel:
                        if seen is not None:
                            box = self.shape_box(obj, [obj] if d is self.cuts else obj[0])[0]
                            if box is None or box[0] > seen[2] or box[2] < seen[0] \
                                    or box[1] > seen[3] or box[3] < seen[1]:
                                continue
                        for o in ([obj] if d is self.cuts else obj[0]):
                            lines.append(([q for x, y in o for q in (pad + (x - x0) * k, pad + (y - y0) * k)],
                                          colour))
        for coords, _ in lines:
            self.line(coords, fill=GLOW, width=8, capstyle='round', joinstyle='round', tags='sel')
        for coords, _ in lines:         # a dark gap between glow and shape, so any ink stays readable
            self.line(coords, fill=COLOURS['canvas'], width=4, capstyle='round', joinstyle='round', tags='sel')
        for coords, colour in lines:
            self.line(coords, fill=screen_ink(colour), width=2, tags='sel')

    def line(self, coords, **options):
        """canvas.create_line(*coords, **options), with the coordinates handed to Tk as one list:
        the same line, several times faster than one argument per number (big drawings)."""
        c = self.canvas
        return c.tk.call(c._w, 'create', 'line', coords, *[a for k, v in options.items() for a in ('-' + k, v)])

    def update_selection_bar(self):
        if not getattr(self, 'sel_label', None):
            return
        n = len(self.selection)
        note = getattr(self, '_note', None)
        self.sel_label.configure(text=note or (f'{n} selected: click a colour' if n
                                               else 'Click or drag to select shapes'))
        self.recolour_button.state(['!disabled'] if n else ['disabled'])
        self.undo_button.state(['!disabled'] if self.history else ['disabled'])

    def fill_recolour_menu(self):
        m = self.recolour_menu
        m.delete(0, 'end')
        colours = sorted(set(self.cuts) | set(self.fills))
        for colour in colours:
            kinds = [k for k, d in (('cut', self.cuts), ('engrave', self.fills)) if colour in d]
            m.add_command(label=f'{colour}  ({" + ".join(kinds)})', command=lambda c=colour: self.recolour(c))
        if colours:
            m.add_separator()
        m.add_command(label='New colour…', command=self.recolour_new)

    def recolour_new(self):
        _, colour = colorchooser.askcolor(parent=self.root, title='Colour for the selected shapes')
        if colour:
            self.recolour(colour.lower())

    @busy_cursor()
    def recolour(self, colour):
        """Move the selected shapes to `colour`. A colour new to the drawing starts as Cut (filled
        shapes: cut along their edges). A colour already there keeps its rows' jobs and the shapes follow them:
        filled shapes moved to a colour with only a cut row become outlines, closed outlines moved to
        one with only a fill row become filled shapes (open lines are cut there, or left alone when
        that row engraves, as they can't be engraved)."""
        if not self.selection:
            self._note = 'Select shapes first: click or drag in the preview'
            self.update_selection_bar()
            return
        current = {c for d in (self.cuts, self.fills) for c, v in d.items() for x in v if id(x) in self.selection}
        if current == {colour}:
            self._note = f'Already {colour}'
            self.update_selection_bar()
            return
        try:
            self.remember()                 # keep what was typed in the rows
        except Exception:
            pass
        keys = set(self.rows) | {('cut', colour), ('engrave', colour)}
        modes = {k: self.memory[k[0]].get(k[1], {}).get('mode') for k in keys}
        has_cut, has_fill = colour in self.cuts, colour in self.fills
        fill_mode = self.rows[('engrave', colour)]['mode'] if has_fill else None
        move = set(self.selection)
        left = 0
        if has_fill and not has_cut and fill_mode == 'engrave':
            for c, v in self.cuts.items():
                for p in v:
                    if id(p) in move and not closed(p):
                        move.discard(id(p))
                        left += 1
        if not move:
            self._note = f'Open lines can\'t be engraved: {colour} engraves'
            self.update_selection_bar()
            return
        self.history.append(({c: list(v) for c, v in self.cuts.items()},
                             {c: list(v) for c, v in self.fills.items()}, modes))
        lines, areas = [], []
        for d, moved in ((self.cuts, lines), (self.fills, areas)):
            for c in list(d):
                keep = [x for x in d[c] if id(x) not in move]
                moved += [x for x in d[c] if id(x) in move]
                if keep:
                    d[c] = keep
                else:
                    del d[c]
        if has_cut and not has_fill:        # filled shapes follow the cut row: cut along their edges
            lines += [list(r) if closed(r) else r + [r[0]] for rings, _ in areas for r in rings]
            areas = []
        elif has_fill and not has_cut:      # closed outlines follow the fill row
            areas += [([p], 'evenodd') for p in lines if closed(p)]
            lines = [p for p in lines if not closed(p)]
        for d, moved in ((self.cuts, lines), (self.fills, areas)):
            if moved:
                d.setdefault(colour, []).extend(moved)
        for kind, d in (('cut', self.cuts), ('engrave', self.fills)):
            if colour in d and not (has_cut if kind == 'cut' else has_fill):
                self.memory[kind].setdefault(colour, {})['mode'] = 'cut'     # a new row starts as Cut
        self.selection = (self.selection - move) | {id(x) for x in lines + areas}
        n = len(move)
        self._note = f'Moved {n} shape{"s" if n != 1 else ""} to {colour}'
        if left:
            self._note += f' ({left} open line{"s" if left != 1 else ""} left: can\'t be engraved)'
        self.reorder()

    @busy_cursor()
    def undo(self):
        if not self.history:
            return
        try:
            self.remember()
        except Exception:
            pass
        self.cuts, self.fills, modes = self.history.pop()
        for (kind, colour), mode in modes.items():         # rows' jobs as they were
            if mode is not None:
                self.memory[kind].setdefault(colour, {})['mode'] = mode
            elif colour in self.memory[kind]:
                self.memory[kind][colour].pop('mode', None)
        self.reorder()

    def reorder(self):
        """After recolouring: new rows for the colours, and the preview redrawn now."""
        self._boxes = {}
        self.count_points()
        self.order = svg2mol.run_order(self.cuts, self.fills)
        self.fill_table()
        self.update_selection_bar()
        if self._estimate_job:
            self.root.after_cancel(self._estimate_job)
        self.update_estimate()

    def save_copy(self):
        base, ext = os.path.splitext(os.path.basename(self.path))
        ext = '.dxf' if ext.lower() == '.dxf' else '.svg'
        path = file_dialogs.asksaveasfilename(
            parent=self.root, initialfile=f'{base}_recoloured{ext}', defaultextension=ext,
            initialdir=os.path.dirname(self.path),
            filetypes=[('SVG', '*.svg'), ('DXF', '*.dxf')] if ext == '.svg' else [('DXF', '*.dxf'), ('SVG', '*.svg')])
        if not path:
            return
        if os.path.abspath(path) == os.path.abspath(self.path):
            messagebox.showerror(APP, 'Choose a new name: the original drawing is never overwritten.', parent=self.root)
            return
        try:
            with self.busy():
                drawing_out.save(path, self.cuts, self.fills)
        except Exception as e:
            messagebox.showerror(APP, f'Could not save {os.path.basename(path)}:\n\n{e}', parent=self.root)
            return
        names = {'text': 'text objects', 'image': 'images'}
        left_out = [f'{n} {names.get(what, what)}' for what, n in self.skipped.items() if n]
        messagebox.showinfo(APP, f'Saved {os.path.basename(path)}: every shape in its current colour '
                                 '(outline = cut, fill = engrave), curves as fine segments.'
                            + (f'\n\nNot included (the laser skips them): {", ".join(left_out)}.' if left_out else ''),
                            parent=self.root)

    # ------------------------------------------------------------ saving

    def file_name(self):
        name = self.name.get().strip().upper()
        if name.endswith('.MOL'):
            name = name[:-4]
        if not SHORT_NAME.fullmatch(name):
            raise ValueError('Use a file name of up to 8 letters, digits or _ (the machine can\'t show longer names).')
        return name + '.MOL'

    def mol_bytes(self):
        """The opened .MOL's bytes exactly as opened (and previewed). Raises ValueError if the file
        on disk has changed since: what is shown would no longer be what is sent."""
        mol = self.mol
        data = mol['data']
        if hashlib.sha256(data).digest() != mol['sha256']:
            raise ValueError('The opened job is damaged in memory; open it again.')
        try:
            with open(mol['path'], 'rb') as f:
                now = f.read()
        except OSError:
            now = data                  # gone (stick unplugged): what was opened is still here
        if now != data:
            raise ValueError(f'{os.path.basename(mol["path"])} has changed since it was opened. '
                             'Open it again to see and send the new version.')
        return data

    def mol_report(self):
        mol = self.mol
        lines = [f'{os.path.basename(mol["path"])} as it is ({len(mol["data"])} bytes, unchanged).']
        if mol.get('size'):
            lines.append(f'Design size: {mol["size"][0]:.1f} x {mol["size"][1]:.1f} mm')
        return lines

    def copy_mol(self, path):
        """Save the opened .MOL's exact bytes to `path` and check the copy reads back the same."""
        try:
            data = self.mol_bytes()
            with self.busy():
                if not (os.path.exists(path) and os.path.samefile(path, self.mol['path'])):
                    with open(path, 'wb') as f:
                        f.write(data)
                with open(path, 'rb') as f:
                    if f.read() != data:
                        raise OSError('the saved copy reads back different from the job; try again')
        except (ValueError, OSError) as e:
            messagebox.showerror(APP, f'Could not save {os.path.basename(path)}:\n\n{e}', parent=self.root)
            return None
        self.remember()
        return None, self.mol_report()

    def write(self, path):
        if self.mol is not None:
            return self.copy_mol(path)
        try:
            with self.busy():
                job, report, _, _ = self.build()
                job.write(path)
        except (ValueError, svg2mol.ConversionError) as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return None
        self.remember()
        return job, report

    def save_usb(self):
        sticks = usb_sticks()
        if not sticks:
            messagebox.showerror(APP, 'No USB stick found. Plug it in and try again.', parent=self.root)
            return
        try:
            fname = self.file_name()
        except ValueError as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return
        stick_name, stick = sticks[0]
        target = os.path.join(stick, fname)
        if os.path.exists(target) and not messagebox.askyesno(
                APP, f'{fname} is already on {stick_name}. Replace it?', parent=self.root):
            return
        result = self.write(target)
        if not result:
            return
        clean_stick(stick)
        msg = self.summary(result, f'Saved {fname} to {stick_name}.')
        if self.eject.get():
            with self.busy():
                ok = subprocess.run(['diskutil', 'eject', stick], capture_output=True).returncode == 0
            msg += '\n\nThe stick has been ejected; you can unplug it.' if ok else \
                '\n\nCould not eject the stick; eject it in Finder before unplugging.'
        else:
            msg += '\n\nEject the stick in Finder before unplugging.'
        messagebox.showinfo(APP, msg, parent=self.root)

    def save_file(self):
        try:
            fname = self.file_name()
        except ValueError as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return
        o = self.memory['options']
        path = file_dialogs.asksaveasfilename(parent=self.root, initialfile=fname, defaultextension='.MOL',
                                               initialdir=o.get('save_dir') or os.path.dirname(self.path),
                                               filetypes=[('Leetro job', '*.MOL *.mol')])
        if not path:
            return
        o['save_dir'] = os.path.dirname(path)
        result = self.write(path)
        if result:
            messagebox.showinfo(APP, self.summary(result, f'Saved {os.path.basename(path)}.'), parent=self.root)

    def send_laser(self):
        """Send the job over the laser's USB cable, deleting the files on it first (like LaserCut).
        An opened .MOL is sent byte for byte as it is."""
        try:
            if self.mol is not None:
                data = self.mol_bytes()         # never rebuilt: the file's own bytes
                result, what = (None, self.mol_report()), f'{os.path.basename(self.path)} as it is'
            else:
                with self.busy():
                    job, report, _, _ = self.build()
                data, result, what = job.to_bytes(), (job, report), 'this job'
        except (ValueError, svg2mol.ConversionError) as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return
        io, denied = None, False
        try:
            io = leetro_usb.UsbIO()
            link = leetro_usb.Link(io)
            _, count = leetro_usb.status(link)
            first = (f'Delete the {count} file(s) on the laser and send {what}?' if count
                     else f'Send {what} to the laser?')
            if not messagebox.askyesno(APP, f'{first}\n\nMake sure the laser is not running a job. '
                                            'It will run the new job when you start it on the panel.',
                                       parent=self.root):
                return
            self.root.configure(cursor='watch')
            self.root.update()
            leetro_usb.send_file(link, data, delete=True)
        except leetro_usb.AccessDenied:     # Linux: no udev rule yet (nothing was sent)
            denied = True
        except Exception as e:      # LeetroError, or a USB error from pyusb
            messagebox.showerror(APP, f'Could not send to the laser:\n\n{e}', parent=self.root)
            return
        finally:
            self.root.configure(cursor='')
            if io:
                try:
                    io.close()
                except Exception:
                    pass
        if denied:
            if self.allow_usb():
                self.send_laser()
            return
        self.remember()
        messagebox.showinfo(APP, self.summary(result, 'Sent to the laser. Start it on the panel.'),
                            parent=self.root)

    def allow_usb(self):
        """Linux: the laser is there but this user may not open it. Offer to install the udev rule
        (the desktop asks for the password); True to try sending again."""
        if linux_usb_access.rule_installed():
            messagebox.showerror(APP, 'Could not send to the laser:\n\nThis computer is allowed to use the laser, '
                                      "but it still refuses. Unplug the laser's USB cable, plug it back in "
                                      'and try again.', parent=self.root)
            return False
        if not messagebox.askyesno(APP, "This computer isn't allowed to use the laser yet. Allow it?\n\n"
                                        '(Asks for your password once.)', parent=self.root):
            return False
        ok, msg = linux_usb_access.install_rule(wait=lambda done: file_dialogs._wait(self.root, done))
        if not ok:
            messagebox.showerror(APP, msg, parent=self.root)
            return False
        return messagebox.askretrycancel(APP, "This computer may now use the laser. Unplug the laser's USB "
                                              'cable, plug it back in, then click Retry to send.', parent=self.root)

    def summary(self, result, first):
        job, report = result
        secs = job.estimate_seconds() if job is not None else (self.mol or {}).get('seconds')
        return '\n'.join([first, ''] + report
                          + (['', f'Estimated time: {secs // 60} min {secs % 60:02d} s'] if secs is not None else []))

    def close(self):
        for job in (self._estimate_job, self._redraw_job):
            if job:
                self.root.after_cancel(job)
        try:
            if getattr(self, 'rows', None) or self.mol is not None:
                self.remember()
            else:
                save_memory(self.memory)
        except Exception:
            pass
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('drawing', nargs='?')
    ap.add_argument('--title', help='name shown in the window title')
    ap.add_argument('--check-usb', action='store_true', help='say whether USB support works and the laser is seen, then quit')
    ap.add_argument('--check-tk', action='store_true', help='say whether this Tk is new enough, then quit (0 = OK)')
    args, _ = ap.parse_known_args()     # the app bundle may pass extra -psn_ arguments
    if args.check_tk:
        r = tk.Tcl()
        have = tk_version(r)
        print(f'Tk {".".join(map(str, have))}: ' + ('OK' if have >= MIN_TK else
              f'too old, need {".".join(map(str, MIN_TK))} or newer (see DEVELOPMENT.md)'))
        sys.exit(0 if have >= MIN_TK else 1)
    if args.check_usb:
        print(f'{TITLE}: {leetro_usb.usb_check()}')
        return
    root = tk.Tk()
    problem = tk_problem(root)
    if problem:
        print(problem, file=sys.stderr)
        root.withdraw()
        messagebox.showerror(APP, problem)
        root.destroy()
        sys.exit(1)
    dark_theme(root)
    win = LaserWindow(root, args.drawing, args.title)
    root.protocol('WM_DELETE_WINDOW', win.close)
    # files dropped on the app icon or opened with it from Finder
    root.createcommand('::tk::mac::OpenDocument', lambda *paths: paths and win.open(paths[0]))
    root.createcommand('::tk::mac::Quit', win.close)
    root.lift()
    root.attributes('-topmost', True)
    root.after(300, lambda: root.attributes('-topmost', False))
    root.focus_force()
    root.mainloop()


if __name__ == '__main__':
    main()
