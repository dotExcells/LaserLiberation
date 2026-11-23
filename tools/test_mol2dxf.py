#!/usr/bin/env python3
"""Check mol2dxf on every cut job in the repo: .MOL -> DXF -> dxf_load + svg2mol -> .MOL must give
the same layers, settings and points (within 1 step = 0.01 mm). Needs the project venv (ezdxf)."""
import contextlib
import glob
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import openmol_path  # noqa: E402,F401  (puts OpenMOL/tools on the path)
from molreader import extract  # noqa: E402
from molwriter import CutLayer  # noqa: E402

try:
    from ezdxf import colors
    import dxf_load
    import mol2dxf
    import svg2mol
except (ImportError, SystemExit):
    sys.exit('needs ezdxf and svgelements: run with the project venv (.venv/bin/python)')

ROOT = os.path.join(os.path.dirname(__file__), '..')
FILES = ([os.path.join(ROOT, 'OpenMOL', 'LASER.MOL')]
         + sorted(glob.glob(os.path.join(ROOT, 'OpenMOL', 'laser decode', '**', 'LASER.MOL'), recursive=True))
         + sorted(glob.glob(os.path.join(ROOT, 'tests', '*.MOL'))))


def settings(layer):
    return layer.speed, layer.power, layer.corner_power, layer.overcut


def relative(layer):
    """Paths as sorted point lists relative to the layer's top-left (the rebuilt job starts there)."""
    x0, y0, _, _ = layer.bbox()
    return sorted(sorted((x - x0, y - y0) for x, y in p) for p in layer.paths)


def rebuild(mol, base):
    """Run mol2dxf, then load the DXF the way Laser Prepare does and build a job from it."""
    orig = [layer for layer in extract(open(mol, 'rb').read())[0] if isinstance(layer, CutLayer)]
    with contextlib.redirect_stdout(io.StringIO()):
        mol2dxf.main([mol, '-o', base, '-f'])
    cuts, fills, _ = dxf_load.load(base + '.dxf')
    chosen = {}
    for n, layer in enumerate(orig):
        r, g, b = colors.aci2rgb(mol2dxf.ACI[n % len(mol2dxf.ACI)])
        chosen[('cut', f'#{r:02x}{g:02x}{b:02x}')] = {
            'speed': mol2dxf.speed_mm_s(layer.speed), 'power': layer.power / 100,
            'corner': layer.corner_power / 100}
    order = svg2mol.run_order(cuts, fills, list(chosen))
    job, _ = svg2mol.build_job(cuts, fills, chosen, order)
    return orig, extract(job.to_bytes())[0]


def compare(orig, new):
    if len(orig) != len(new):
        return [f'{len(orig)} layers -> {len(new)}']
    problems = []
    # svg2mol may run nested colours first, so match layers by their settings, not their order
    for a, b in zip(sorted(orig, key=settings), sorted(new, key=settings)):
        if settings(a) != settings(b):
            problems.append(f'settings {settings(a)} -> {settings(b)}')
            continue
        pa, pb = relative(a), relative(b)
        if [len(p) for p in pa] != [len(p) for p in pb]:
            problems.append(f'{sum(map(len, pa))} points in {len(pa)} paths -> {sum(map(len, pb))} in {len(pb)}')
            continue
        worst = max(max(abs(p[0] - q[0]), abs(p[1] - q[1])) for P, Q in zip(pa, pb) for p, q in zip(P, Q))
        if worst > 1:
            problems.append(f'points moved by up to {worst} steps')
    return problems


def main():
    all_ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for mol in FILES:
            orig, new = rebuild(mol, os.path.join(tmp, 'job'))
            name = os.path.relpath(mol, ROOT)
            if not orig:
                print(f'{name:45s} engrave only, skipped')
                continue
            problems = compare(orig, new)
            all_ok &= not problems
            print(f'{name:45s} {"OK" if not problems else "DIFF " + "; ".join(problems)}')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
