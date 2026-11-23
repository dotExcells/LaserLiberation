#!/usr/bin/env python3
"""Check jobs built with each start origin (MolJob(origin=...)): every move is the top-left job's,
offset by the head's new position; the preview, the header box, the Frame routines and reading
back (molreader, mol2dxf) all put the head at the chosen corner or centre.
mol2dxf's check needs ezdxf (skipped without it)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from mol_inspect import records  # noqa: E402
from molreader import extract, job_box, job_origin, nested  # noqa: E402
from molwriter import ORIGINS, STEPS_PER_MM as S, CutLayer, EngraveLayer, MolJob  # noqa: E402
import mol_preview  # noqa: E402
from test_roundtrip import sections  # noqa: E402

try:
    import mol2dxf
except (ImportError, SystemExit):
    mol2dxf = None


def steps(points):
    return [(round(x * S), round(y * S)) for x, y in points]


# in mm from the design's top-left: a 20 mm square with its top-left corner cut off, a 4 mm
# square hole, and an engraved 6 x 4 mm block reaching past the square's right edge (so the
# design box isn't the square's)
SQUARE = steps([(3, 0), (20, 0), (20, 20), (0, 20), (0, 3), (3, 0)])
HOLE = steps([(4, 10), (8, 10), (8, 14), (4, 14), (4, 10)])
BLOCK = [(y, [(round(16 * S), round(22 * S))]) for y in range(round(4 * S), round(8 * S), 11)]
BLOCK_BOX = (round(16 * S), round(4 * S), round(22 * S), round(8 * S))
# the head's offset from the top-left start, in steps (box 0..22 x 0..20 mm)
EXPECTED = {'top-left': (0, 0), 'top-right': (2444, 0), 'bottom-left': (0, 2222),
            'bottom-right': (2444, 2222), 'center': (1222, 1111)}
NAMES = {'top-left': 'top-left corner', 'top-right': 'top-right corner',
         'bottom-left': 'bottom-left corner', 'bottom-right': 'bottom-right corner', 'center': 'centre'}


def build(origin):
    job = MolJob([EngraveLayer(BLOCK, 100, 20, bbox=BLOCK_BOX), CutLayer([SQUARE, HOLE], 10, 15, 10)],
                 origin=origin)
    return job, job.to_bytes()


def shifted(points, d):
    return sorted((x - d[0], y - d[1]) for x, y in points)


def frame_moves(data, sid):
    """Head positions (steps, +y down, from the head) along Frame section sid, and laser-on spans."""
    pos, pts, laser, on = (0, 0), [(0, 0)], False, []
    inside = False
    for off, op, a in records(data):
        if op == 0x300C0B:
            inside = a[0] == sid
        elif inside and op == 0x8000080B:
            for nop, na in nested(a):
                if nop == 0x220:
                    laser = na[0] == 1
                elif nop == 0x20500:
                    new = (pos[0] + na[0], pos[1] - na[1])
                    if laser:
                        on.append((pos, new))
                    pos = new
                    pts.append(pos)
    return pts, on


def main():
    problems = []
    ref_job, ref = build('top-left')
    ref_travel, ref_cuts, ref_eng = mol_preview.decode(ref)
    ref_cut_pts = shifted([p for ls in ref_cuts.values() for l in ls for p in l], (0, 0))
    ref_eng_pts = shifted([p for es in ref_eng.values() for s in es for p in s], (0, 0))
    ref_box = job_box(ref)
    for origin in ORIGINS:
        bad = []
        job, data = build(origin)
        d = tuple(h - r for h, r in zip(job.head(), ref_job.head()))
        if d != EXPECTED[origin]:
            bad.append(f'head at {d}, expected {EXPECTED[origin]}')
        # preview: cuts and engraving are the top-left job's, moved by -d (head at 0,0)
        travel, cuts, eng = mol_preview.decode(data)
        if shifted([p for ls in cuts.values() for l in ls for p in l], (-d[0], -d[1])) != ref_cut_pts:
            bad.append('cut moves are not the top-left job\'s, offset')
        if shifted([p for es in eng.values() for s in es for p in s], (-d[0], -d[1])) != ref_eng_pts:
            bad.append('engrave passes are not the top-left job\'s, offset')
        if travel[0][0] != (0, 0):
            bad.append('preview does not start at the head')
        # the square's notched corner is where the design says, relative to the head
        notch = (SQUARE[0][0] - d[0], SQUARE[0][1] - d[1])
        if not any(notch in l for ls in cuts.values() for l in ls):
            bad.append(f'notch corner not at {notch} steps from the head')
        # header box and the origin read back from it
        box = job_box(data)
        if box != (ref_box[0] - d[0], ref_box[1] - d[1], ref_box[2] - d[0], ref_box[3] - d[1]):
            bad.append(f'header box {box}')
        if job_origin(data) != origin:
            bad.append(f'header reads back as origin {job_origin(data)}')
        # Frame: laser-off trace visits the box corners and ends at its first corner (the
        # head's, or the top-left for the centre, like LaserCut); laser-on frame traces the box
        # 2 mm larger, from the corner beyond the head's
        pts, _ = frame_moves(data, 1)
        x0, y0, x1, y1 = box
        corners = {(x0, y0), (x1, y0), (x0, y1), (x1, y1)}
        if not corners <= set(pts) or pts[-1] != ((x0, y0) if origin == 'center' else (0, 0)):
            bad.append(f'frame trace {pts}')
        if origin != 'center' and pts[1:5] and pts[0] not in corners:
            bad.append('frame trace does not start on a corner')
        _, on = frame_moves(data, 2)
        fx = sorted({p[0] for seg in on for p in seg})
        fy = sorted({p[1] for seg in on for p in seg})
        if (fx, fy) != ([x0 - 223, x1 + 222], [y0 - 222, y1 + 223]) or len(on) != 4:
            bad.append(f'laser-on frame {on}')
        # molreader: the layers come back relative to the head and rebuild the same moves and
        # frames (not the engrave layer: the reader doesn't get its return-pass speed back)
        layers, canvas = extract(data)
        for again in (MolJob(layers, canvas, origin=(0, 0)), MolJob(layers, canvas, origin=origin)):
            a, b = sections(data), sections(again.to_bytes())
            diff = [k for k in ('main', 'frame', 'frame_cut', 'layer4') if a[k] != b[k]]
            if a['header'][20:40] != b['header'][20:40]:
                diff.append('header box')
            if diff:
                bad.append(f'rebuilt from molreader (origin={again.origin!r}): {", ".join(diff)} differ')
        # mol2dxf: the settings text names the start
        if mol2dxf:
            mm = [{'box': tuple(v / S for v in l.bbox())} for l in layers]
            start = mol2dxf.start_text(mol2dxf.design_box(mm))
            if NAMES[origin] not in start:
                bad.append(f'mol2dxf says the head was at {start!r}')
        print(f'{origin:13s} {"OK" if not bad else "DIFF " + "; ".join(bad)}')
        problems += bad
    if not mol2dxf:
        print('mol2dxf skipped (needs ezdxf)')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
