#!/usr/bin/env python3
"""Draw what a .MOL job will do as an SVG: cut paths, engraved lines and travel moves.

Usage: mol_preview.py LASER.MOL [-o preview.svg]

Works on LaserCut's files and ours. Coordinates start at the head position (in
immediate mode the design's top-left, or the corner chosen with svg2mol.py
--origin), shown as a green dot.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from mol_inspect import records  # noqa: E402
from molreader import BLOCK, flat_records  # noqa: E402

STEPS_PER_MM = 111.111111
CUT_COLOURS = ['#d62728', '#1f77b4', '#2ca02c', '#9467bd', '#ff7f0e', '#8c564b']


def decode(data):
    """Return travel moves, cut polylines per layer and engrave segments per layer (steps, +y down)."""
    recs = list(records(data))
    calls, sections = [], {}
    sid, stream = None, []
    for off, op, a in recs:
        if op == 0x300C0B:
            sid = a[0]
            sections[sid] = []
        elif op == 0x400C0B:
            if sid is not None and sid >= 3:
                sections[sid] += flat_records(stream)
            sid, stream = None, []
        elif op == BLOCK and sid is not None and sid >= 3:
            stream += a
        elif sid is None and off < 0x400:
            if op == 0x30500:
                calls.append(('rapid', a[1], -a[2]))
            elif op == 0x8050000B and 3 <= a[0] < 603:
                calls.append(('call', a[0]))
        elif sid is not None and sid >= 3:
            sections[sid].append((op, a))
    pos, travel, cuts, engraves = (0, 0), [], {}, {}
    for c in calls:
        if c[0] == 'rapid':
            new = (pos[0] + c[1], pos[1] + c[2])
            travel.append((pos, new))
            pos = new
            continue
        layer, recs_ = c[1], sections[c[1]]
        over, pend, laser = 0, None, False
        cur = [pos]
        for op, a in recs_:
            if op == 0x620:
                over = a[0]
            elif op == 0x220:
                laser = a[0] == 1
                if not laser and len(cur) > 1:
                    cuts.setdefault(layer, []).append(cur)
                cur = [pos]
            elif op == 0x20500:
                pos = (pos[0] + a[1], pos[1] - a[2])
                cur.append(pos)
            elif op == 0x30500:
                new = (pos[0] + a[1], pos[1] - a[2])
                travel.append((pos, new))
                pos = new
                cur = [pos]
            elif op == 0x80000720:
                pend = [w & 0xFFFFFFFF for w in a]
            elif op == 0x40500:
                new = (pos[0], pos[1] - a[1])
                pos = new
            elif op == 0x14000:
                dx = a[1]
                d = 1 if dx > 0 else -1
                r0, p = pos[0] + d * over, 0
                for w in pend[1:] if pend and pend[1:] != [0x250000, 0] else []:
                    on, of = w & 0xFFFF, w >> 16
                    if on:
                        x0 = r0 + d * p
                        engraves.setdefault(layer, []).append(((x0, pos[1]), (x0 + d * on, pos[1])))
                    p += on + of
                travel.append((pos, (pos[0] + dx, pos[1])))
                pos = (pos[0] + dx, pos[1])
        if len(cur) > 1:
            cuts.setdefault(layer, []).append(cur)
    return travel, cuts, engraves


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mol')
    ap.add_argument('-o', '--output')
    ap.add_argument('--no-travel', action='store_true', help='hide travel / overscan moves')
    args = ap.parse_args()
    data = open(args.mol, 'rb').read()
    travel, cuts, engraves = decode(data)
    pts = [p for seg in travel for p in seg] + [p for ls in cuts.values() for l in ls for p in l] \
        + [p for es in engraves.values() for s in es for p in s] + [(0, 0)]
    S = STEPS_PER_MM
    x0, y0 = min(p[0] for p in pts) / S - 2, min(p[1] for p in pts) / S - 2
    x1, y1 = max(p[0] for p in pts) / S + 2, max(p[1] for p in pts) / S + 2
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{(x1 - x0) * 4:.0f}" height="{(y1 - y0) * 4:.0f}" '
           f'viewBox="{x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f}">',
           f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="white"/>']
    if not args.no_travel:
        d = ' '.join(f'M{a[0] / S:.2f},{a[1] / S:.2f}L{b[0] / S:.2f},{b[1] / S:.2f}' for a, b in travel)
        out.append(f'<path d="{d}" stroke="#bbb" stroke-width="0.08" fill="none" stroke-dasharray="0.4 0.4"/>')
    for i, (layer, segs) in enumerate(sorted(engraves.items())):
        d = ' '.join(f'M{a[0] / S:.2f},{a[1] / S:.3f}H{b[0] / S:.2f}' for a, b in segs)
        out.append(f'<path d="{d}" stroke="#222" stroke-width="0.07" fill="none"/>')
    for i, (layer, lines) in enumerate(sorted(cuts.items())):
        col = CUT_COLOURS[i % len(CUT_COLOURS)]
        for l in lines:
            d = 'M' + 'L'.join(f'{x / S:.3f},{y / S:.3f}' for x, y in l)
            out.append(f'<path d="{d}" stroke="{col}" stroke-width="0.15" fill="none"/>')
    out.append(f'<circle cx="0" cy="0" r="0.8" fill="#2ca02c"/>')
    out.append('</svg>')
    path = args.output or os.path.splitext(args.mol)[0] + '_preview.svg'
    with open(path, 'w') as f:
        f.write('\n'.join(out))
    n_e = sum(len(v) for v in engraves.values())
    n_c = sum(len(v) for v in cuts.values())
    print(f'wrote {path}: {len(cuts)} cut layers ({n_c} paths), {len(engraves)} engrave layers ({n_e} burn segments)')


if __name__ == '__main__':
    main()
