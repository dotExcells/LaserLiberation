#!/usr/bin/env python3
"""Read a LaserCut 5.3 project (.ECP) and its laser settings (.INI next to it).

Reverse-engineered from one project (a business card: engraving + cut, LaserCut 5.3). See
MOL_FORMAT.md, "LaserCut project files (.ECP)". The .ECP holds the drawing; the settings per
layer colour are in an .INI with the same name, which LaserCut writes when it saves the project.

Usage: ecpreader.py FILE.ECP     print what's in it
"""
import configparser
import glob
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from cfb import CompoundFile, NotCfb  # noqa: E402

GRAPHICS = 'Ec.EcDbGraphics.1'
JOIN_MM = 1e-6


class EcpError(ValueError):
    pass


def tokens(data):
    """The EC stream is a flat run of tagged values (Windows VARIANT types)."""
    i, out = 0, []
    while i < len(data):
        t = struct.unpack_from('<H', data, i)[0]
        i += 2
        if t in (2, 0x0B):        # int16, bool
            v = struct.unpack_from('<h', data, i)[0]
            i += 2
        elif t == 3:              # int32
            v = struct.unpack_from('<i', data, i)[0]
            i += 4
        elif t == 4:              # float32
            v = struct.unpack_from('<f', data, i)[0]
            i += 4
        elif t == 5:              # float64
            v = struct.unpack_from('<d', data, i)[0]
            i += 8
        elif t == 0x11:           # byte
            v = data[i]
            i += 1
        elif t == 8:              # string: byte count, then UTF-16 (ANSI/GBK text widened per byte)
            n = struct.unpack_from('<I', data, i)[0]
            raw = data[i + 4:i + 4 + n]
            i += 4 + n
            v = raw.decode('utf-16-le', 'replace').rstrip('\0')
            if all(ord(c) < 256 for c in v):
                try:
                    v = bytes(ord(c) for c in v).decode('gbk')
                except UnicodeDecodeError:
                    pass
        else:
            raise EcpError(f'unknown value type {t:#x} at byte {i - 2:#x}')
        out.append((t, v))
    return out


def bgr(c):
    """LaserCut colours are Windows COLORREF (0x00BBGGRR) -> (r, g, b)."""
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


def object_paths(obj, index):
    """Polylines (mm, LaserCut canvas, +y up) of one graphics object, and how many tokens they use.
    Kinds seen: 1 line, 5 polyline of segments (segment kind 1 = straight), 7 point list."""
    kind = obj[0][1]
    if kind == 1:
        x0, y0, x1, y1 = (v for _, v in obj[1:5])
        return [[(x0, y0), (x1, y1)]], 6
    if kind == 7:
        n = obj[1][1]
        vals = [v for _, v in obj[2:2 + 2 * n]]
        return [list(zip(vals[0::2], vals[1::2]))], 2 + 2 * n + 1
    if kind == 5:
        n, k, paths = obj[1][1], 2, []
        for s in range(n):
            seg = obj[k][1]
            if seg != 1:
                raise EcpError(f'object {index}: curve segment type {seg} is not supported yet '
                               '(only straight segments seen so far)')
            x0, y0, x1, y1 = (v for _, v in obj[k + 1:k + 5])
            if paths and abs(paths[-1][-1][0] - x0) < JOIN_MM and abs(paths[-1][-1][1] - y0) < JOIN_MM:
                paths[-1].append((x1, y1))
            else:
                paths.append([(x0, y0), (x1, y1)])
            k += 6
        return paths, k + 1
    raise EcpError(f'object {index}: unknown object type {kind}')


TAIL_LAYER = 39     # an object's layer number is this many values after its geometry


def find_ini(ecp_path):
    """LaserCut saves the settings as NAME.INI next to NAME.ECP (case may differ)."""
    base = os.path.splitext(os.path.basename(ecp_path))[0].lower()
    for p in glob.glob(os.path.join(os.path.dirname(ecp_path) or '.', '*')):
        stem, ext = os.path.splitext(os.path.basename(p))
        if ext.lower() == '.ini' and stem.lower() == base:
            return p
    return None


def read_ini(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    with open(path, encoding='latin-1') as f:
        cp.read_file(f)
    out = {'origin': None, 'layers': {}}
    if cp.has_section('Technics_Laser_Data'):
        d = cp['Technics_Laser_Data']
        if 'LogicOrgPositionX' in d:
            out['origin'] = (float(d['LogicOrgPositionX']), float(d['LogicOrgPositionY']))
    for sec in cp.sections():
        if sec.startswith('TechnicsLayers_'):
            s = cp[sec]
            mode = s.get('TechnicsMode', '')
            f = lambda key, default=0.0: float(s.get(key, default))  # noqa: E731
            if mode == 'TECHNICS_LASERENGRAVE':
                st = {'mode': 'engrave', 'speed': f('LaserEngraveSpeed'), 'power': f('LaserEngravePower'),
                      'power2': f('LaserEngravePower2'), 'interval': f('LaserEngraveScanStepLen'),
                      'bidir': s.get('LaserEngraveTwoDirector', '0') != '0'}
            elif mode == 'TECHNICS_LASERCUT':
                st = {'mode': 'cut', 'speed': f('LaserCutSpeed'), 'power': f('LaserCutWorkPower'),
                      'corner': f('LaserCutConnerPower')}
            else:
                st = {'mode': mode or 'unknown'}
            out['layers'][int(sec.split('_', 1)[1])] = st
    return out


def read(path, ini_path=None):
    """{'layers': [{'number', 'colour', 'rgb', 'name', 'paths', 'settings'}], 'origin', 'ini'}.
    Paths are in mm in LaserCut's canvas (+y up). settings is None when there is no .INI."""
    data = open(path, 'rb').read()
    try:
        stream = CompoundFile(data).read('EC')
    except (NotCfb, KeyError):
        raise EcpError('not a LaserCut .ECP project (no EC stream)')
    toks = tokens(stream)
    starts = [k for k, (t, v) in enumerate(toks) if t == 8 and v == GRAPHICS]
    if not starts:
        raise EcpError('no drawing objects found')

    table = {}            # layer number -> (name, COLORREF)
    for k in range(starts[0]):
        t, v = toks[k]
        if t == 8 and k + 12 < len(toks) and toks[k + 1][0] == 2 and toks[k + 12][0] == 3:
            table.setdefault(toks[k + 1][1], (v, toks[k + 12][1]))

    shapes = {}
    for n, a in enumerate(starts):
        b = starts[n + 1] if n + 1 < len(starts) else len(toks)
        obj = toks[a + 1:b]
        if obj[0][1] == 0:          # the document's root object
            continue
        paths, used = object_paths(obj, n)
        tail = obj[used:]
        if len(tail) < TAIL_LAYER + 1 or tail[TAIL_LAYER][0] != 3:
            raise EcpError(f'object {n}: unexpected layout after its geometry')
        shapes.setdefault(tail[TAIL_LAYER][1], []).extend(p for p in paths if len(p) > 1)

    ini_path = ini_path or find_ini(path)
    ini = read_ini(ini_path) if ini_path else {'origin': None, 'layers': {}}
    layers = []
    for number in sorted(shapes):
        name, colour = table.get(number, ('', 0))
        layers.append({'number': number, 'name': name, 'colour': colour, 'rgb': bgr(colour),
                       'paths': shapes[number],
                       'settings': ini['layers'].get(colour) if ini_path else None})
    return {'layers': layers, 'origin': ini['origin'], 'ini': ini_path}


def main():
    job = read(sys.argv[1])
    print(f'settings: {job["ini"] or "no .INI found"}; origin {job["origin"]}')
    for layer in job['layers']:
        r, g, b = layer['rgb']
        print(f'layer {layer["number"]} {layer["name"]!r} #{r:02x}{g:02x}{b:02x}: '
              f'{len(layer["paths"])} paths, {layer["settings"]}')


if __name__ == '__main__':
    main()
