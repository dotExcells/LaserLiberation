#!/usr/bin/env python3
"""Check the both-direction return-pass shift: by default it follows LaserCut's Acc space table
(-0.20 mm up to 199 mm/s, -0.25 to 399, -0.40 to 599, -0.35 above), an explicit shift wins,
and the shift written is the one read back."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from molreader import extract  # noqa: E402
from molwriter import STEPS_PER_MM as S, EngraveLayer, MolJob, acc_space  # noqa: E402

TABLE = [(1, 0.20), (100, 0.20), (199, 0.20), (199.9, 0.20), (200, 0.25), (399, 0.25),
         (400, 0.40), (500, 0.40), (599, 0.40), (600, 0.35), (1000, 0.35)]
STEPS = {0.20: 22, 0.25: 28, 0.40: 44, 0.35: 39}   # L_bidir (100 mm/s) has 22
SQUARE = [(y * 11, [(0, 1667)]) for y in range(20)]   # 15 mm wide


def main():
    bad = []
    for speed, mm in TABLE:
        if acc_space(speed) != mm:
            bad.append(f'acc_space({speed}) = {acc_space(speed)}, expected {mm}')
        layer = EngraveLayer(SQUARE, speed, 15, bidirectional=True)
        if layer.shift != STEPS[mm]:
            bad.append(f'{speed} mm/s: shift {layer.shift} steps, expected {STEPS[mm]}')
        back = extract(MolJob([layer], (0, 0)).to_bytes())[0][0]
        if back.shift != layer.shift:
            bad.append(f'{speed} mm/s: read back {back.shift} steps, wrote {layer.shift}')
    if EngraveLayer(SQUARE, 500, 15, bidirectional=True, bidir_shift_mm=0.2).shift != 22:
        bad.append('explicit 0.2 mm at 500 mm/s is not 22 steps')
    if EngraveLayer(SQUARE, 500, 15, bidirectional=True, bidir_shift_mm=0).shift != 0:
        bad.append('explicit 0 mm is not 0 steps')
    for line in bad:
        print('FAIL', line)
    print('bidir shift:', 'OK' if not bad else f'{len(bad)} failures')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
