#!/usr/bin/env python3
"""Check jobs built with each start origin (svg2mol --origin): every move is the top-left job's,
offset by the head's new position; the preview, the header box, the Frame routines and reading
back (molreader, mol2dxf) all put the head at the chosen corner or centre; all five are offered.
Needs the project venv (svgelements, ezdxf)."""
import contextlib
import io
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import openmol_path  # noqa: E402,F401  (puts OpenMOL/tools on the path)
from mol_inspect import records  # noqa: E402
from molreader import extract, job_box, job_origin, nested  # noqa: E402
from molwriter import ORIGINS, STEPS_PER_MM as S, MolJob  # noqa: E402
import mol_preview  # noqa: E402
from test_roundtrip import sections  # noqa: E402

try:
    import mol2dxf
    import svg2mol
    import test_mol2dxf
except (ImportError, SystemExit):
    sys.exit('needs ezdxf and svgelements: run with the project venv (.venv/bin/python)')

# a 20 mm square with its top-left corner cut off, a 4 mm square hole, and an engraved
# 6 x 4 mm block reaching past the square's right edge (so the design box isn't the square's)
SQUARE = [(13, 10), (30, 10), (30, 30), (10, 30), (10, 13), (13, 10)]
HOLE = [(14, 20), (18, 20), (18, 24), (14, 24), (14, 20)]
BLOCK = [(26, 14), (32, 14), (32, 18), (26, 18), (26, 14)]
CUTS = {'#ff0000': [SQUARE, HOLE]}
FILLS = {'#000000': [([BLOCK], 'nonzero')]}
SETTINGS = {('cut', '#ff0000'): {'speed': 10, 'power': 15, 'corner': 10},
            ('engrave', '#000000'): {'speed': 100, 'power': 20}}
ORDER = [('engrave', '#000000'), ('cut', '#ff0000')]
EXPECTED_MM = {'top-left': (10, 10), 'top-right': (32, 10), 'bottom-left': (10, 30),
               'bottom-right': (32, 30), 'center': (21, 20)}


def build(origin):
    job, report = svg2mol.build_job(CUTS, FILLS, SETTINGS, ORDER, origin=origin)
    return job, job.to_bytes(), report


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


def left_out():
    """A colour left out of the job keeps its place (LaserCut does the same for objects not output):
    with `design` the box, start and Frame are the whole drawing's and the square that is cut
    stays where it is relative to the head. Giving the job's own shapes as `design` changes
    nothing."""
    bad = []
    circle = [(10 + 10 * math.cos(a * math.pi / 18), 20 + 10 * math.sin(a * math.pi / 18)) for a in range(37)]
    square = [(40, 10), (60, 10), (60, 30), (40, 30), (40, 10)]
    whole = ({'#0000ff': [circle], '#ff0000': [square]}, {})
    cuts, settings = {'#ff0000': [square]}, {('cut', '#ff0000'): SETTINGS[('cut', '#ff0000')]}
    order = [('cut', '#ff0000')]
    for origin in ORIGINS:
        plain = svg2mol.build_job(cuts, {}, settings, order, origin=origin)[0].to_bytes()
        same = svg2mol.build_job(cuts, {}, settings, order, origin=origin, design=(cuts, {}))[0].to_bytes()
        if plain != same:
            bad.append(f'{origin}: own shapes as design change the job')
        job, report = svg2mol.build_job(cuts, {}, settings, order, origin=origin, design=whole)
        data = job.to_bytes()
        hx, hy = svg2mol.origin_point(*whole, origin)
        x0, y0 = (round((0 - hx) * S), round((10 - hy) * S))
        # (within a step: the head sits on the step grid)
        near = lambda a, b: all(abs(p - q) <= 1 for p, q in zip(a, b))  # noqa: E731
        if not near(job_box(data), (x0, y0, x0 + round(60 * S), y0 + round(20 * S))):
            bad.append(f'{origin}: header box {job_box(data)}')
        _, got, _ = mol_preview.decode(data)
        corner = (round((40 - hx) * S), round((10 - hy) * S))
        if not any(near(p, corner) for ls in got.values() for l in ls for p in l):
            bad.append(f'{origin}: square not at {corner} steps from the head')
        if job_origin(data) != origin:
            bad.append(f'{origin}: header reads back as origin {job_origin(data)}')
        if not report[0].startswith('Design size: 60.0 x 20.0'):
            bad.append(f'{origin}: report {report[0]}')
    print(f'left out      {"OK" if not bad else "DIFF " + "; ".join(bad)}')
    return bad


def main():
    problems = []
    _, ref, _ = build('top-left')
    ref_travel, ref_cuts, ref_eng = mol_preview.decode(ref)
    ref_cut_pts = shifted([p for ls in ref_cuts.values() for l in ls for p in l], (0, 0))
    ref_eng_pts = shifted([p for es in ref_eng.values() for s in es for p in s], (0, 0))
    ref_box = job_box(ref)
    with tempfile.TemporaryDirectory() as tmp:
        for origin in ORIGINS:
            bad = []
            job, data, report = build(origin)
            mm = svg2mol.origin_point(CUTS, FILLS, origin)
            if mm != EXPECTED_MM[origin]:
                bad.append(f'origin_point {mm}, expected {EXPECTED_MM[origin]}')
            # the head's offset from the top-left start, in steps
            d = (round((mm[0] - 10) * S), round((mm[1] - 10) * S))
            # preview: cuts and engraving are the top-left job's, moved by -d (head at 0,0)
            travel, cuts, eng = mol_preview.decode(data)
            if shifted([p for ls in cuts.values() for l in ls for p in l], (-d[0], -d[1])) != ref_cut_pts:
                bad.append('cut moves are not the top-left job\'s, offset')
            if shifted([p for es in eng.values() for s in es for p in s], (-d[0], -d[1])) != ref_eng_pts:
                bad.append('engrave passes are not the top-left job\'s, offset')
            if travel[0][0] != (0, 0):
                bad.append('preview does not start at the head')
            # the square's notched corner is where the design says, relative to the head
            notch = (round((13 - mm[0]) * S), round((10 - mm[1]) * S))
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
            # mol2dxf: settings text names the start, DXF comes back as the same shapes
            mol = os.path.join(tmp, 'job.MOL')
            with open(mol, 'wb') as f:
                f.write(data)
            want = svg2mol.ORIGIN_NAMES[origin]
            start = mol2dxf.start_text(mol2dxf.design_box(mol2dxf.read_mol(mol)['layers']))
            if want not in start:
                bad.append(f'mol2dxf says the head was at {start!r}')
            with contextlib.redirect_stdout(io.StringIO()):
                orig, new = test_mol2dxf.rebuild(mol, os.path.join(tmp, 'job'))
            bad += test_mol2dxf.compare(orig, new)
            if ('start:' in report[1]) != (origin != 'top-left'):
                bad.append(f'report {report}')
            print(f'{origin:13s} {"OK" if not bad else "DIFF " + "; ".join(bad)}')
            problems += bad
    problems += left_out()
    # every start is offered (the centre since its machine test, 2026-10-21)
    offered = set(svg2mol.OFFERED_ORIGINS)
    print(f'offered: {", ".join(sorted(offered))}')
    if offered != set(ORIGINS):
        problems.append(f'offered origins {sorted(offered)}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
