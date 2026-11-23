#!/usr/bin/env python3
"""Turn a .MOL job or a LaserCut .ECP project back into a drawing: NAME.dxf plus
NAME_settings.txt with the laser settings.

Usage: mol2dxf.py JOB.MOL|PROJECT.ECP [-o OUTNAME] [--force] [--ini SETTINGS.INI]

.MOL: works on LaserCut's files (old jobs from the old PC) and ours. Curves come back as the short
straight segments the machine cuts (~0.1 mm); the overcut LaserCut adds at the end of closed
shapes is removed. Engrave layers are listed in the text file but not drawn (a .MOL only has the
scan lines).

.ECP: LaserCut's own project. The drawing is exact (as drawn in LaserCut), engrave areas included;
the settings come from the .INI LaserCut saves next to it (same name).

Each layer becomes its own DXF layer and colour, named after its settings, so the DXF can go
straight back into Laser Prepare / laser_window.py with the settings from the text file: cut
layers are polylines, engrave layers are filled areas (HATCH). Units are mm; the design's top-left
corner is the DXF origin and the design lies below it (DXF +y is up), whatever corner the job
started from. The text file says where the original job expected the head (a corner, the centre,
or a fixed place on the bed).
"""
import argparse
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
import ecpreader  # noqa: E402
from molreader import NotAMolFile, check, extract  # noqa: E402
from molwriter import ORIGINS, STEPS_PER_MM, CutLayer, origin_point  # noqa: E402

try:
    import ezdxf
    from ezdxf import colors
except ImportError:
    sys.exit('ezdxf is missing: run with the project venv (.venv/bin/python) or `pip install ezdxf`.')

S = STEPS_PER_MM
# Colours that are easy to tell apart, then the rest of AutoCAD's palette
ACI = [1, 5, 3, 6, 30, 4, 2, 8] + [i for i in range(10, 250, 20)] + [i for i in range(14, 250, 20)]
CLOSE_MM = 0.01


def speed_mm_s(steps_s):
    """The shortest mm/s value that gives these steps/s (the file stores int(mm/s * 111.111))."""
    for digits in range(4):
        v = round(steps_s / S, digits)
        if int(v * S) == int(steps_s):
            return v
    return steps_s / S


def num(v):
    return f'{v:g}'


def closed(p):
    return len(p) > 2 and math.dist(p[0], p[-1]) < CLOSE_MM


def path_length(p):
    return sum(math.dist(a, b) for a, b in zip(p, p[1:]))


# ---------------------------------------------------------------- reading
# Both readers give the same job: {'source', 'layers', 'time', 'start', 'notes'}. A layer is
# {'kind': 'cut' | 'engrave' | other, 'settings': {...} or None, 'rgb': (r, g, b) or None,
#  'paths': cut polylines, 'fills': engrave outlines (closed rings) or None, 'lines': scan lines,
#  'box': (x0, y0, x1, y1) of what it covers}, all in mm, +y down, relative to the head's start.

def box_of(points):
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def read_mol(path):
    with open(path, 'rb') as f:
        data = f.read()
    check(data)
    layers = []
    for layer in extract(data)[0]:
        if isinstance(layer, CutLayer):
            paths = [[(x / S, y / S) for x, y in p] for p in layer.paths]
            layers.append({'kind': 'cut', 'rgb': None, 'paths': paths, 'fills': None,
                           'box': box_of([q for p in paths for q in p]),
                           'settings': {'speed': speed_mm_s(layer.speed), 'power': layer.power / 100,
                                        'corner': layer.corner_power / 100, 'overcut': layer.overcut}})
        else:
            pts = [(x / S, y / S) for y, spans in layer.lines for span in spans for x in span]
            layers.append({'kind': 'engrave', 'rgb': None, 'paths': [], 'fills': None, 'lines': len(layer.lines),
                           'box': box_of(pts),
                           'settings': {'speed': speed_mm_s(layer.speed), 'power': layer.power / 100,
                                        'power2': layer.power2 / 100, 'bidir': layer.bidirectional,
                                        'overscan': layer.overscan / S}})
    secs = struct.unpack_from('<i', data, 48)[0]
    return {'source': path, 'layers': layers, 'time': secs, 'start': 'head', 'notes': []}


def read_ecp(path, ini=None):
    job = ecpreader.read(path, ini)
    everything = [q for layer in job['layers'] for p in layer['paths'] for q in p]
    x0, _, _, y1 = box_of(everything)      # canvas is +y up: the top is the largest y
    ox, oy = job['origin'] or (x0, y1)
    layers, notes = [], []
    if not job['ini']:
        notes.append('No .INI with the same name was found next to the .ECP, so the laser settings '
                      'are unknown (use --ini FILE to give it).')
    # LaserCut runs engraving first; the .ECP doesn't store the order of the other layers
    for layer in sorted(job['layers'], key=lambda l: ((l['settings'] or {}).get('mode') != 'engrave', l['number'])):
        st = dict(layer['settings'] or {})
        kind = st.pop('mode', 'unknown')
        paths = [[(x - ox, oy - y) for x, y in p] for p in layer['paths']]
        entry = {'kind': kind, 'rgb': layer['rgb'], 'settings': st or None, 'number': layer['number'],
                 'paths': paths, 'fills': None, 'box': box_of([q for p in paths for q in p])}
        if kind == 'engrave':
            entry['fills'] = [p for p in paths if closed(p)]
            entry['paths'] = []
            n_open = len(paths) - len(entry['fills'])
            if n_open:
                notes.append(f'Layer {layer["number"]}: {n_open} open shapes on an engrave layer left out '
                             '(only closed shapes can be filled).')
        layers.append(entry)
    start = 'head' if job['origin'] else None
    return {'source': path, 'layers': layers, 'time': None, 'start': start, 'notes': notes}


def read_job(path, ini=None):
    with open(path, 'rb') as f:
        magic = f.read(8)
    if magic == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return read_ecp(path, ini)
    return read_mol(path)


# ---------------------------------------------------------------- writing

def layer_name(i, layer):
    st, kind = layer['settings'], layer['kind']
    if not st:
        return f'{i}_LAYER{layer.get("number", "")}'
    name = f'{i}_{kind.upper()}_{num(st["speed"])}mms_{num(st["power"])}pct'
    if kind == 'cut' and st['corner'] != st['power']:
        name += f'_corner{num(st["corner"])}pct'
    return name


def colour_of(layer, n):
    """(rgb, aci): the original LaserCut colour when known, else a palette colour."""
    if layer['rgb'] is not None:
        return layer['rgb'], None
    aci = ACI[n % len(ACI)]
    return colors.aci2rgb(aci), aci


def design_box(layers):
    boxes = [l['box'] for l in layers if l['box']]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def drawn(layer):
    return bool(layer['paths'] or layer['fills'])


def write_dxf(job, names, path, origin):
    doc = ezdxf.new('R2010', setup=False)
    doc.header['$INSUNITS'] = 4          # mm
    doc.header['$MEASUREMENT'] = 1
    msp = doc.modelspace()
    ox, oy = origin

    def pts(p):
        return [(x - ox, -(y - oy)) for x, y in p]
    for layer in job['layers']:
        if not drawn(layer):
            continue
        name, rgb, aci = names[id(layer)]
        dl = doc.layers.add(name, color=aci or 7)
        if aci is None:
            dl.rgb = rgb
        for p in layer['paths']:
            c = closed(p)
            msp.add_lwpolyline(pts(p[:-1] if c else p), close=c, dxfattribs={'layer': name})
        if layer['fills']:
            hatch = msp.add_hatch(color=256, dxfattribs={'layer': name})   # 256 = colour by layer
            hatch.dxf.hatch_style = 0     # odd parity: holes in letters stay holes
            for ring in layer['fills']:
                hatch.paths.add_polyline_path(pts(ring[:-1]), is_closed=True)
    doc.saveas(path)


def start_text(box):
    """Where the original job expected the head, relative to the design."""
    x0, y0, x1, y1 = box
    near = 0.5
    for name in ORIGINS:
        if all(abs(v) <= near for v in origin_point(box, name)):
            if name == 'center':
                return 'the centre of the design'
            return f"the design's {name} corner" + (' (Laser Prepare\'s default)' if name == 'top-left' else '')
    dx, dy = -x0, -y0
    return (f"{abs(dx):.1f} mm {'right' if dx >= 0 else 'left'} of and {abs(dy):.1f} mm "
            f"{'below' if dy >= 0 else 'above'} the design's top-left corner "
            "(LaserCut placed the job on the bed, not at the head)")


def settings_text(job, names):
    x0, y0, x1, y1 = box = design_box(job['layers'])
    ecp = job['time'] is None
    out = [f'Laser settings from {os.path.basename(job["source"])}', '',
           f'Design size: {x1 - x0:.2f} x {y1 - y0:.2f} mm (width x height)']
    if not ecp:
        secs = job['time']
        out.append(f'Job time:    about {f"{secs // 60} min " if secs >= 60 else ""}{secs % 60} s '
                   "(the file's own estimate)")
    if job['start']:
        what = 'job' if not ecp else 'project (LaserCut origin)'
        out.append(f'Start:       the original {what} started with the head at {start_text(box)}.')
    out += ["             In the DXF the design's top-left corner is at 0,0.",
            f'Layers:      {len(job["layers"])}, ' + (
                'run in this order' if not ecp else 'engraving first like LaserCut; the .ECP does not '
                'store the order of the cut layers'),
            '']
    for i, layer in enumerate(job['layers'], 1):
        name, rgb, aci = names[id(layer)]
        st, kind = layer['settings'], layer['kind']
        head = f'{i}. {kind.upper():8s}'
        if not drawn(layer):
            head += ' (not in the DXF: a .MOL only has the scan lines, not the shapes)'
        else:
            head += f' DXF layer {name}'
        out.append(head)
        if layer.get('number') is not None:
            out.append(f'   LaserCut layer {layer["number"]}')
        if drawn(layer):
            out.append(f'   colour        #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}'
                       + (f' (AutoCAD colour {aci})' if aci else ''))
        if not st:
            out.append('   settings      unknown (no .INI)')
        elif kind == 'cut':
            out += [f'   speed         {num(st["speed"])} mm/s',
                    f'   power         {num(st["power"])} %',
                    f'   corner power  {num(st["corner"])} %']
            if 'overcut' in st:
                out.append('   overcut       ' + ('yes: closed shapes run 0.1-0.2 mm past their start'
                                                  if st['overcut'] else 'none'))
        elif kind == 'engrave':
            out += [f'   speed         {num(st["speed"])} mm/s',
                    f'   power         {num(st["power"])} %  (second power {num(st["power2"])} %)',
                    f'   direction     {"both" if st["bidir"] else "one"}']
            if 'interval' in st:
                out.append(f'   line spacing  {num(st["interval"])} mm')
            if 'overscan' in st:
                out.append(f'   overscan      {st["overscan"]:.1f} mm')
        if layer['paths']:
            n_closed = sum(1 for p in layer['paths'] if closed(p))
            out.append(f'   shapes        {len(layer["paths"])} ({n_closed} closed), '
                       f'{sum(path_length(p) for p in layer["paths"]):.1f} mm of cutting')
        if layer['fills']:
            out.append(f'   shapes        {len(layer["fills"])} outlines, filled')
        if layer.get('lines'):
            out.append(f'   lines         {layer["lines"]}')
        out.append('')
    out += job['notes']
    return '\n'.join(out).rstrip() + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('job', help='.MOL job or .ECP project')
    ap.add_argument('-o', '--output', metavar='OUTNAME',
                    help='name for the output files without extension (default: next to the input)')
    ap.add_argument('-f', '--force', action='store_true', help='overwrite existing output files')
    ap.add_argument('--ini', help='.ECP only: the settings file, if it is not NAME.INI next to the project')
    args = ap.parse_args(argv)

    try:
        job = read_job(args.job, args.ini)
    except (NotAMolFile, ecpreader.EcpError) as e:
        sys.exit(f'{args.job}: {e}')
    except OSError as e:
        sys.exit(f'{e.filename}: {e.strerror}')
    base = args.output or os.path.splitext(args.job)[0]
    if base.lower().endswith('.dxf'):
        base = base[:-4]
    dxf_path, txt_path = base + '.dxf', base + '_settings.txt'
    any_drawn = any(drawn(layer) for layer in job['layers'])
    outputs = ([dxf_path] if any_drawn else []) + [txt_path]
    existing = [p for p in outputs if os.path.exists(p)]
    if existing and not args.force:
        sys.exit(f'not overwriting {", ".join(existing)} (use --force)')

    names, n = {}, 0
    for i, layer in enumerate(job['layers'], 1):
        rgb, aci = colour_of(layer, n)
        if drawn(layer) and aci:
            n += 1
        names[id(layer)] = (layer_name(i, layer), rgb, aci)
    if any_drawn:
        x0, y0, _, _ = design_box(job['layers'])
        write_dxf(job, names, dxf_path, (x0, y0))
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(settings_text(job, names))

    print(f'{args.job}: {len(job["layers"])} layers')
    for layer in job['layers']:
        what = (f'{len(layer["paths"])} shapes' if layer['paths'] else
                f'{len(layer["fills"])} filled outlines' if layer['fills'] else 'not drawn (scan lines only)')
        print(f'  {names[id(layer)][0]}: {what}')
    for note in job['notes']:
        print('  ' + note)
    print(f'wrote {dxf_path}' if any_drawn else 'nothing to draw: no DXF written')
    print(f'wrote {txt_path}')


if __name__ == '__main__':
    main()
