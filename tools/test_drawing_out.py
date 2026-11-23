#!/usr/bin/env python3
"""Check drawing_out: load each test drawing, recolour some shapes, save the copy as SVG and as DXF,
read it back with svg2mol.load / dxf_load.load, and compare colours, shapes and points.
Needs the project venv (svgelements, ezdxf)."""
import glob
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import drawing_out  # noqa: E402
import dxf_load  # noqa: E402
import svg2mol  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')
TOL = 1e-3      # mm


def load(path):
    return (dxf_load.load if path.lower().endswith('.dxf') else svg2mol.load)(path)[:2]


def recolour(cuts, fills):
    """Move every other shape of the first colour of each kind to a new colour, like the window."""
    cuts = {c: list(v) for c, v in cuts.items()}
    fills = {c: list(v) for c, v in fills.items()}
    for d, new in ((cuts, '#00a0ff'), (fills, '#808000')):
        if d:
            first = next(iter(d))
            moved = d[first][::2]
            d[first] = d[first][1::2]
            d.setdefault(new, []).extend(moved)
            if not d[first]:
                del d[first]
    return cuts, fills


def dedupe(p):
    """Drop repeated points (zero-length segments, which DXF reading drops too)."""
    return [q for i, q in enumerate(p) if i == 0 or math.dist(q, p[i - 1]) >= TOL]


def same_polys(a, b):
    """Same polylines in any order, points within TOL (closed rings may start anywhere)."""
    a, b = [dedupe(p) for p in a], [dedupe(p) for p in b]
    def key(p):
        return (len(p), round(min(x for x, _ in p), 2), round(min(y for _, y in p), 2))
    a, b = sorted(a, key=key), sorted(b, key=key)
    if len(a) != len(b):
        return f'{len(a)} shapes -> {len(b)}'
    for p, q in zip(a, b):
        if len(p) != len(q):
            return f'a shape with {len(p)} points came back with {len(q)}'
        if not all(any(math.dist(u, v) < TOL for v in q) for u in p):
            return 'points moved'
    return None


def compare(cuts, fills, cuts2, fills2, check_rule):
    problems = []
    if set(cuts) != set(cuts2) or set(fills) != set(fills2):
        problems.append(f'colours {sorted(cuts)}/{sorted(fills)} -> {sorted(cuts2)}/{sorted(fills2)}')
        return problems
    for c in cuts:
        if (p := same_polys(cuts[c], cuts2[c])):
            problems.append(f'cut {c}: {p}')
    for c in fills:
        rings = [r for shape, _ in fills[c] for r in shape]
        rings2 = [r for shape, _ in fills2[c] for r in shape]
        if (p := same_polys(rings, rings2)):
            problems.append(f'fill {c}: {p}')
        if check_rule and sorted(r for _, r in fills[c]) != sorted(r for _, r in fills2[c]):
            problems.append(f'fill {c}: fill rules changed')
    return problems


def main():
    ok = True
    drawings = sorted(glob.glob(os.path.join(ROOT, 'tests', '*.svg')) + glob.glob(os.path.join(ROOT, 'tests', '*.dxf')))
    with tempfile.TemporaryDirectory() as tmp:
        for path in drawings:
            cuts, fills = recolour(*load(path))
            for ext in ('svg', 'dxf'):
                out = os.path.join(tmp, f'copy.{ext}')
                drawing_out.save(out, cuts, fills)
                # DXF joins touching open lines and has no fill rules (dxf_load reads even-odd)
                if ext == 'dxf':
                    want_cuts = {c: dxf_load.join_paths(v) for c, v in cuts.items()}
                else:
                    want_cuts = cuts
                problems = compare(want_cuts, fills, *load(out), check_rule=ext == 'svg')
                ok &= not problems
                name = os.path.relpath(path, ROOT)
                print(f'{name:45s} -> {ext}: {"OK" if not problems else "DIFF " + "; ".join(problems)}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
