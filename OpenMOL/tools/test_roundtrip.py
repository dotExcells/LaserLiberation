#!/usr/bin/env python3
"""Rebuild sample cut files with molwriter and compare them to LaserCut's output."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from molreader import extract  # noqa: E402
from molwriter import MolJob  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')
# Sections we knowingly approximate: the panel Frame routines (corner speeds, margin rounding)
# and travel-move peak speeds that can land one 8 steps/s grid step below LaserCut's.
APPROX = {'frame', 'frame_cut'}
# Engrave passes can end 1-2 steps (0.01-0.02 mm) short of LaserCut's inside the overscan.
KNOWN = {('E_circle', 'main'), ('G_engrave', 'main'), ('I_new_engrave', 'main'), ('L_bidir', 'main'),
         ('G_engrave', 'layer3'), ('I_new_engrave', 'layer3'), ('J_engrave_circle', 'layer3'),
         ('L_bidir', 'layer3')}
KNOWN |= {('M_origins/tr', 'main'), ('M_origins/centre', 'main'), ('M_origins/centre_top', 'main'),
          ('M_origins/centre center', 'main'),  # rapid peak; also the head 1 step off (canvas rounding)
          ('M_origins/tr', 'layer3')}            # overcut 12 steps instead of 11 (canvas rounding)
SAMPLES = ['A_square', 'B_speed', 'C_Power', 'H_new_corner', 'E_circle', 'F_two_layers',
           'G_engrave', 'I_new_engrave', 'J_engrave_circle', 'K_engrave_bars', 'L_bidir']
# Start positions (top-left, top-right, centre, centre top): rebuilt with the head where the sample
# has it, and the centre once more with origin='center' (our own rounding of the head). Not
# M_origins/tl_non_relative: our writer only writes jobs relative to the head.
STARTS = [('M_origins/tl', (0, 0)), ('M_origins/tr', (0, 0)), ('M_origins/centre', (0, 0)),
          ('M_origins/centre_top', (0, 0)), ('M_origins/centre', 'center')]


def sections(data):
    n = struct.unpack_from('<I', data, 0)[0]
    table = [b for b in struct.unpack_from('<16I', data, 0x70) if b]
    edges = table + [n // 0x200]
    names = ['main', 'frame', 'frame_cut'] + [f'layer{i + 3}' for i in range(len(table) - 3)]
    return {'header': data[:0x200], **{nm: data[a * 0x200:b * 0x200]
                                       for nm, a, b in zip(names, edges, edges[1:])}}


def main():
    all_ok = True
    for name, start in [(n, None) for n in SAMPLES] + STARTS:
        orig = open(os.path.join(ROOT, 'laser decode', name, 'LASER.MOL'), 'rb').read()
        layers, canvas = extract(orig)
        new = MolJob(layers, canvas, **({} if start is None else {'origin': start})).to_bytes()
        if isinstance(start, str):
            name += ' ' + start
        so, sn = sections(orig), sections(new)
        report = []
        for key in so:
            a, b = so[key], sn.get(key, b'')
            if a == b:
                report.append(f'{key}=OK')
                continue
            if key == 'header':
                wa = struct.unpack_from('<13i', a)
                wb = struct.unpack_from('<13i', b)
                diff = [f'w{i}:{x}->{y}' for i, (x, y) in enumerate(zip(wa, wb)) if x != y]
                report.append('header[' + ','.join(diff) + ']')
                continue
            nd = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            tag = 'approx' if key in APPROX or (name, key) in KNOWN else 'DIFF'
            report.append(f'{key}={tag}({nd}B @+{first:#x})')
            if tag == 'DIFF':
                all_ok = False
        print(f'{name:23s}', ' '.join(report))
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
