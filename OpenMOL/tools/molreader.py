#!/usr/bin/env python3
"""Read a .MOL job back into molwriter layers (CutLayer / EngraveLayer, steps, +y down).

Works on LaserCut's files and ours. Used by test_roundtrip.py and mol2dxf.py.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from mol_inspect import leetro_float, records  # noqa: E402
from molwriter import ORIGINS, STEPS_PER_MM, CutLayer, EngraveLayer, origin_point  # noqa: E402

VERSION = 0x04030000


class NotAMolFile(ValueError):
    pass


def check(data):
    """Raise NotAMolFile unless data looks like a Leetro .MOL job."""
    if len(data) < 0x400 or struct.unpack_from('<I', data, 8)[0] != VERSION:
        raise NotAMolFile('not a Leetro .MOL file (unknown header)')
    if struct.unpack_from('<I', data, 0)[0] != len(data):
        raise NotAMolFile('file size in the header does not match: file is cut short or damaged')


BLOCK = 0x8000080B   # planned-motion block: its payload is a list of nested records


def nested(payload):
    """Yield (opcode, args) from a planned-motion block payload (top byte = arg count).
    Seen nested: 0x101 speed profile, 0x3000C [L, h], 0x20500 [dx, dy, L], 0x220 laser on/off,
    0x105 laser enable, 0x608 wait."""
    i = 0
    while i < len(payload):
        w = payload[i] & 0xFFFFFFFF
        n = w >> 24
        yield w & 0xFFFFFF, payload[i + 1:i + 1 + n]
        i += 1 + n


OVERCUT_MAX = 30   # steps; LaserCut's overcut is 0.1 mm (11 steps), some versions ~0.2 mm


def overcut_start(p):
    """Index where a closed path gets back to its start before LaserCut's overcut tail, or None.
    The tail usually is one segment; on curves (and in older LaserCut files) it follows the
    first segments of the path for a few points."""
    for j in range(len(p) - 2, 1, -1):
        tail = sum(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 for (ax, ay), (bx, by) in zip(p[j:], p[j + 1:]))
        if tail > OVERCUT_MAX:
            return None
        if p[j] == p[0]:
            return j
    return None


def extract_engrave(data, recs, sid, start):
    """Recover an engrave layer: lines (y, spans) in steps (+y down) relative to the origin."""
    inside, settings, pend = False, {}, None
    x, y = start
    passes = []
    for off, op, a in recs:
        if op == 0x300C0B:
            inside = a[0] == sid
            continue
        if not inside:
            continue
        if op == 0x14301:
            settings['speed'] = leetro_float(a[1])
        elif op == 0x620:
            settings['overscan'] = a[0]
        elif op == 0x120 and len(a) == 2:
            settings['power'], settings['power2'] = a
        elif op == 0x80000720:
            pend = [w & 0xFFFFFFFF for w in a]
        elif op == 0x40500:
            y -= a[1]
        elif op == 0x14000:
            dx, over = a[1], settings['overscan']
            words = pend[1:]
            if words != [0x250000, 0]:
                d = 1 if dx > 0 else -1
                r0, pos, spans = x + d * over, 0, []
                for w in words:
                    on, of = w & 0xFFFF, w >> 16
                    if on:
                        spans.append((r0 + pos, r0 + pos + on) if d > 0 else (r0 - pos - on, r0 - pos))
                    pos += on + of
                passes.append((y, d, sorted(spans)))
            x += dx
    bidir = any(d < 0 for _, d, _ in passes)
    shift = 0
    if bidir:  # LaserCut shifts return passes left; measure it from the first two lines
        shift = passes[0][2][0][0] - passes[1][2][0][0]
    lines = [(yy, [(a + shift, b + shift) for a, b in sp] if d < 0 else sp) for yy, d, sp in passes]
    return settings, lines, bidir, shift


def flat_records(stream):
    """A cut layer stored as planned-motion blocks (some LaserCut versions write cuts this way),
    as the flat records our writer uses: laser-on moves -> 0x20500 [0x304, dx, dy, L],
    laser-off moves -> 0x30500 [0x304, dx, dy, L, 0], laser switches -> 0x220."""
    laser = False
    for op, a in nested(stream):
        if op == 0x220:
            laser = a[0] == 1
            yield op, a
        elif op == 0x20500:
            yield (0x20500, [0x304] + a) if laser else (0x30500, [0x304] + a + [0])


def job_box(data):
    """The job box from header words 6-9: (x0, y0, x1, y1) in steps, +y down, relative to the head."""
    right, top, left, bottom = struct.unpack_from('<4i', data, 24)
    return left, -top, right, -bottom


def job_origin(data):
    """Which of ORIGINS the head starts at (from the header's job box), or None if none of them
    (LaserCut jobs placed on the bed)."""
    box = job_box(data)
    for name in ORIGINS:
        if all(abs(v) <= 1 for v in origin_point(box, name)):
            return name
    return None


def extract(data):
    """Recover layers (paths in steps, +y down, relative to the head's start) and canvas origin
    from a .MOL. To rebuild the job, give MolJob origin=(0, 0)."""
    recs = list(records(data))
    origin = None
    calls, layer_data = [], {}
    for off, op, a in recs:
        if off >= 0x400:
            break
        if op == 0x80600101 and a[0] == 603 and origin is None:
            origin = (int(leetro_float(a[5])), int(leetro_float(a[6])))
        elif op == 0x30500:
            calls.append(('rapid', a[1], -a[2]))
        elif op == 0x8050000B and 3 <= a[0] < 603:
            calls.append(('call', a[0]))
    box = job_box(data)
    sid, params, engrave, stream = None, {}, set(), []
    for off, op, a in recs:
        if op == 0x300C0B:
            sid = a[0]
            layer_data[sid] = []
        elif sid is None or sid < 3:
            continue
        elif op == 0x400C0B:
            layer_data[sid] += [(op_ == 0x30500, a_[1], -a_[2]) for op_, a_ in flat_records(stream)
                                if op_ != 0x220]
            sid, stream = None, []
        elif op == 0x14301:
            engrave.add(sid)
        elif op == 0x2009 and sid not in params:
            params[sid] = (a[0], a[1], leetro_float(a[3]))
        elif op in (0x20500, 0x30500):
            layer_data[sid].append((op == 0x30500, a[1], -a[2]))
        elif op == BLOCK:
            stream += a   # a record can continue in the next block
    layers, pos = [], (0, 0)
    for c in calls:
        if c[0] == 'rapid':
            pos = (pos[0] + c[1], pos[1] + c[2])
            continue
        if c[1] in engrave:
            st, lines, bidir, shift = extract_engrave(data, recs, c[1], pos)
            layer = EngraveLayer(lines, st['speed'] / STEPS_PER_MM, st['power'] / 100, st['power2'] / 100,
                                 st['overscan'] / STEPS_PER_MM, bidir, shift / STEPS_PER_MM,
                                 bbox=box)
            layers.append(layer)
            pos = layer.end()
            continue
        paths, cur = [], [pos]
        for is_rapid, dx, dy in layer_data[c[1]]:
            pos = (pos[0] + dx, pos[1] + dy)
            if is_rapid:
                paths.append(cur)
                cur = [pos]
            else:
                cur.append(pos)
        paths.append(cur)
        overcut = False
        for p in paths:
            j = overcut_start(p)
            if j:
                del p[j + 1:]
                overcut = True
        corner, power, speed = params[c[1]]
        layers.append(CutLayer(paths, speed / STEPS_PER_MM, power / 100, corner / 100, overcut))
    return layers, origin
