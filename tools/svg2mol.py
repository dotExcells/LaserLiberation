#!/usr/bin/env python3
"""Convert an SVG (e.g. from Inkscape) into a Leetro .MOL job.

Stroke colours become CUT layers; fill colours become ENGRAVE layers. A shape
with both a stroke and a fill is engraved and cut. Settings per colour:

    svg2mol.py design.svg -o LASER.MOL \\
        --engrave black:speed=100,power=20 \\
        --layer blue:speed=20,power=50,corner=30 --layer red:speed=10,power=90,corner=60

  cut     (--layer):   speed, power, corner
  engrave (--engrave): speed, power, interval (line spacing mm, default 0.1),
                       bidir (1 = burn both directions), shift (bidir offset mm; default: by speed
                       like LaserCut's Acc space, 0.2 up to 199 mm/s, 0.25 / 0.4 / 0.35 above),
                       overscan (mm; default: worked out from speed, LaserCut used 25),
                       return (mm/s for laser-off return passes, 300; 0 = scan speed like LaserCut),
                       power2 (LaserCut's second engrave power, 60)

Or put them in a JSON file and pass --settings layers.json:

    {"engrave": {"black": {"speed": 100, "power": 20}},
     "cut": {"blue": {"speed": 20, "power": 50, "corner": 30}}}

Layers run in the order given (command line, then settings file); any others
follow with engraving first. Speeds are mm/s, powers are percent. The head's
position when the job starts is the top-left corner of the design (immediate mode),
or the corner given with --origin.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import openmol_path  # noqa: E402,F401  (puts OpenMOL/tools on the path)
from molwriter import ORIGINS, STEPS_PER_MM, CutLayer, EngraveLayer, MolJob  # noqa: E402
import molwriter  # noqa: E402

try:
    from svgelements import SVG, Close, Color, Image, Move, Path, Shape, Text
except ImportError:
    sys.exit('svgelements is missing: run with the project venv (.venv/bin/python) '
             'or `pip install svgelements`.')
try:
    import numpy as np
except ImportError:
    np = None

MM_PER_PX = 25.4 / 96          # svgelements reports user units at 96 dpi
SEGMENT_MM = 0.1               # curve flattening resolution (LaserCut used ~0.1 mm)
DEFAULTS = {
    'cut': {'speed': 10.0, 'power': 30.0, 'corner': None},
    'engrave': {'speed': 100.0, 'power': 20.0, 'power2': 60.0, 'interval': 0.1,
                'bidir': 0.0, 'shift': None, 'overscan': None, 'return': 300.0},
}


def spec_parser(kind):
    def parse(text):
        colour, _, rest = text.partition(':')
        settings = {}
        for item in filter(None, rest.split(',')):
            key, _, val = item.partition('=')
            if key not in DEFAULTS[kind]:
                raise argparse.ArgumentTypeError(
                    f'unknown {kind} setting {key!r} in {text!r} (allowed: {", ".join(DEFAULTS[kind])})')
            settings[key] = float(val)
        return kind, Color(colour).hex, settings
    return parse


# ---------------------------------------------------------------- geometry

def cubic_steps(seg):
    """Segments for a cubic Bézier, the same as from seg.length(error=1e-4) but mostly without it.

    That length is a bisection to depth 5 or more ending in half-chords, so it is at least the
    64-chord length (with scipy it is the integral, within its 1e-4 error), and the curve is no
    longer than the control polygons of its 64 pieces. When both give the same count, that's it."""
    if np is None:
        return None
    t = np.linspace(0, 1, 65)
    xy = seg.npoint(t)
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg.start, seg.control1, seg.control2, seg.end
    u = 1 - t       # derivative / 3 at each t, times the piece length 1/64
    d = np.empty_like(xy)
    d[:, 0] = (u * u * (x1 - x0) + 2 * u * t * (x2 - x1) + t * t * (x3 - x2)) / 64
    d[:, 1] = (u * u * (y1 - y0) + 2 * u * t * (y2 - y1) + t * t * (y3 - y2)) / 64
    a, b = xy[:-1] + d[:-1], xy[1:] - d[1:]
    chords = np.hypot(*(xy[1:] - xy[:-1]).T).sum()
    polygons = (np.hypot(*(a - xy[:-1]).T) + np.hypot(*(b - a).T) + np.hypot(*(xy[1:] - b).T)).sum()
    lo = max(1, math.ceil((chords * (1 - 1e-9) - 2e-4) * MM_PER_PX / SEGMENT_MM))
    hi = max(1, math.ceil((polygons * (1 + 1e-9) + 2e-4) * MM_PER_PX / SEGMENT_MM))
    return lo if lo == hi else None


def flatten(path):
    """Yield polylines (lists of (x, y) mm) and whether each is closed."""
    current, closed = [], False
    for seg in path:
        if isinstance(seg, Move):
            if len(current) > 1:
                yield current, closed
            current, closed = [(seg.end.x * MM_PER_PX, seg.end.y * MM_PER_PX)], False
            continue
        if isinstance(seg, Close):
            if current and seg.end is not None:
                current.append((seg.end.x * MM_PER_PX, seg.end.y * MM_PER_PX))
            closed = True
            continue
        kind = seg.__class__.__name__
        n = 1 if kind == 'Line' else cubic_steps(seg) if kind == 'CubicBezier' else None
        if n is None:
            n = max(1, math.ceil(seg.length(error=1e-4) * MM_PER_PX / SEGMENT_MM))
        if n == 1:
            p = seg.point(1.0)
            current.append((float(p.x) * MM_PER_PX, float(p.y) * MM_PER_PX))
            continue
        # all points of a segment in one go (an arc works out its centre once, not per point)
        pts = seg.npoint([i / n for i in range(1, n + 1)])
        pts = pts.tolist() if hasattr(pts, 'tolist') else [(p.x, p.y) for p in pts]
        current += [(x * MM_PER_PX, y * MM_PER_PX) for x, y in pts]
    if len(current) > 1:
        yield current, closed


def visible(colour):
    return colour is not None and colour.value is not None and colour.alpha != 0


def load(svg_file):
    """Return cuts {colour: [polyline]}, fills {colour: [(rings, rule)]}, skipped counts."""
    svg = SVG.parse(svg_file, reify=True)
    cuts, fills, skipped = {}, {}, {'text': 0, 'image': 0}
    for el in svg.elements():
        if el.values.get('visibility') == 'hidden' or el.values.get('display') == 'none':
            continue
        if isinstance(el, Text):
            skipped['text'] += 1
            continue
        if isinstance(el, Image):
            skipped['image'] += 1
            continue
        if not isinstance(el, Shape):
            continue
        path = Path(el)
        if len(path) == 0:
            continue
        polys = list(flatten(path))
        if visible(el.fill):
            rings = [p for p, _ in polys if len(p) > 2]
            if rings:
                rule = el.values.get('fill-rule', 'nonzero')
                fills.setdefault(el.fill.hex, []).append((rings, rule))
        if visible(el.stroke):
            for poly, closed in polys:
                if closed and poly[0] != poly[-1]:
                    poly.append(poly[0])
                cuts.setdefault(el.stroke.hex, []).append(poly)
    return cuts, fills, skipped


def inside(pt, poly):
    """Even-odd point-in-polygon test."""
    x, y = pt
    hit = False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            hit = not hit
    return hit


def bbox(poly):
    xs = [x for x, _ in poly]
    ys = [y for _, y in poly]
    return min(xs), min(ys), max(xs), max(ys)


def inside_box(pt, poly, box):
    """inside(), with a quick reject for points outside the polygon's bounding box."""
    return box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3] and inside(pt, poly)


class BoxIndex:
    """Items (..., poly, box) on a coarse grid by bounding box, so `near(pt)` gives every item whose
    box may hold pt (and others) without going through them all. Boxes over many cells (an outer
    frame) are kept in one list that every lookup includes."""

    def __init__(self, items):
        self.big, self.cells = [], {}
        if not items:
            return
        boxes = [it[-1] for it in items]
        self.x0, self.y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        self.n = max(1, int(math.sqrt(len(items))))
        self.w = (max(b[2] for b in boxes) - self.x0) / self.n or 1.0
        self.h = (max(b[3] for b in boxes) - self.y0) / self.n or 1.0
        for it in items:
            b = it[-1]
            i0, j0 = self.cell(b[0], b[1])
            i1, j1 = self.cell(b[2], b[3])
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 16:
                self.big.append(it)
                continue
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    self.cells.setdefault((i, j), []).append(it)

    def cell(self, x, y):
        n = self.n - 1
        return (min(n, max(0, int((x - self.x0) / self.w))), min(n, max(0, int((y - self.y0) / self.h))))

    def near(self, pt):
        if not self.cells:
            return self.big
        return self.big + self.cells.get(self.cell(*pt), [])


def depth(paths):
    """For each path, how many closed paths enclose it (holes and inner parts score higher)."""
    closed = BoxIndex([(j, q, bbox(q)) for j, q in enumerate(paths) if len(q) > 3 and q[0] == q[-1]])
    out = []
    for i, p in enumerate(paths):
        probe = p[len(p) // 2]
        out.append(sum(1 for j, q, b in closed.near(probe)
                       if j != i and inside_box(probe, q, b) and inside_box(p[0], q, b)))
    return out


def order_paths(paths, start=(0.0, 0.0)):
    """Cut innermost shapes first, then greedy nearest-neighbour to keep travel short."""
    d = depth(paths)
    ordered, pos = [], start
    for level in sorted(set(d), reverse=True):
        for p in nearest_first([p for p, k in zip(paths, d) if k == level], pos):
            ordered.append(p)
            pos = p[-1]
    return ordered


def nearest_first(paths, pos):
    """Yield the paths greedily: next is the one whose start is nearest the last one's end (the
    earlier path on a tie), from `pos`. Start points sit on a grid, searched in rings of cells
    around the current point until no cell further out can hold anything nearer."""
    done = [False] * len(paths)
    left, few, cells, built = len(paths), None, None, 0
    while left:
        if left <= 32:                      # few left: test each (min keeps the earliest on a tie)
            if few is None:
                few = [k for k in range(len(paths)) if not done[k]]
            i = min(few, key=lambda k: math.dist(pos, paths[k][0]))
            few.remove(i)
        else:
            if cells is None or left * 4 < built:       # (re)build for the paths still left
                live = [k for k in range(len(paths)) if not done[k]]
                built = len(live)
                xs = [paths[k][0][0] for k in live]
                ys = [paths[k][0][1] for k in live]
                x0, y0 = min(xs), min(ys)
                n = max(1, int(math.sqrt(built)))
                w = (max(xs) - x0) / n or 1.0
                h = (max(ys) - y0) / n or 1.0

                def cell(x, y):
                    return min(n - 1, max(0, int((x - x0) / w))), min(n - 1, max(0, int((y - y0) / h)))
                cells = {}
                for k in live:
                    cells.setdefault(cell(*paths[k][0]), []).append(k)
            ci, cj = cell(*pos)
            px, py = pos
            best, r = None, 0
            while True:
                for a in range(max(0, ci - r), min(n - 1, ci + r) + 1):
                    edge = a in (ci - r, ci + r)
                    for b in (range(cj - r, cj + r + 1) if edge else (cj - r, cj + r)):
                        for k in cells.get((a, b), ()):
                            dk = math.dist(pos, paths[k][0])
                            if best is None or (dk, k) < best:
                                best = (dk, k)
                # anything not looked at yet lies beyond the searched cells' unclipped sides
                sides = [s for s, clipped in ((px - (x0 + (ci - r) * w), ci - r <= 0),
                                              (x0 + (ci + r + 1) * w - px, ci + r >= n - 1),
                                              (py - (y0 + (cj - r) * h), cj - r <= 0),
                                              (y0 + (cj + r + 1) * h - py, cj + r >= n - 1)) if not clipped]
                if not sides or (best is not None and best[0] < min(sides) - 1e-6):
                    break
                r += 1
            i = best[1]
            cells[cell(*paths[i][0])].remove(i)
        done[i] = True
        left -= 1
        yield paths[i]
        pos = paths[i][-1]


def to_steps(poly):
    pts = []
    for x, y in poly:
        p = (int(round(x * STEPS_PER_MM)), int(round(y * STEPS_PER_MM)))
        if not pts or p != pts[-1]:
            pts.append(p)
    # keep closed paths exactly closed after rounding
    if len(poly) > 2 and poly[0] == poly[-1] and pts[-1] != pts[0]:
        pts.append(pts[0])
    return pts


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def scanlines(shapes, interval):
    """Fill spans per scan line for filled shapes (union across shapes).

    Returns ([(y_mm, [(x0, x1), ...]), ...], bbox_mm). Lines sit at the centre of each
    interval-high band, measured up from the bottom of the shapes like LaserCut.
    """
    xs = [x for rings, _ in shapes for r in rings for x, _ in r]
    ys = [y for rings, _ in shapes for r in rings for _, y in r]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    n = int(round((y1 - y0) / interval))
    ylines = [y1 - (k + 0.5) * interval for k in range(n)]
    # bucket each shape's edges by the scan lines they cross
    per_line = [[] for _ in ylines]
    for si, (rings, rule) in enumerate(shapes):
        for ring in rings:
            pts = ring if ring[0] == ring[-1] else ring + [ring[0]]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if ay == by:
                    continue
                lo, hi = min(ay, by), max(ay, by)
                # line k at y1 - (k + 0.5)·interval; find k with lo <= y < hi
                k_lo = max(0, math.ceil((y1 - hi) / interval - 0.5))
                k_hi = min(n - 1, math.floor((y1 - lo) / interval - 0.5))
                for k in range(k_lo, k_hi + 1):
                    y = ylines[k]
                    if lo <= y < hi:
                        x = ax + (y - ay) * (bx - ax) / (by - ay)
                        per_line[k].append((si, x, 1 if by > ay else -1))
    lines = []
    for y, hits in zip(ylines, per_line):
        spans = []
        for si in {h[0] for h in hits}:
            rule = shapes[si][1]
            xs_dir = sorted((x, d) for s, x, d in hits if s == si)
            wind, start = 0, None
            for x, d in xs_dir:
                was_in = (wind % 2 != 0) if rule == 'evenodd' else wind != 0
                wind += 1 if rule == 'evenodd' else d
                now_in = (wind % 2 != 0) if rule == 'evenodd' else wind != 0
                if now_in and not was_in:
                    start = x
                elif was_in and not now_in:
                    spans.append((start, x))
        if spans:
            lines.append((y, merge(spans)))
    return lines, (x0, y0, x1, y1)


# ---------------------------------------------------------------- job building

ORIGIN_NAMES = {'top-left': 'top-left corner', 'top-right': 'top-right corner',
                'bottom-left': 'bottom-left corner', 'bottom-right': 'bottom-right corner',
                'center': 'centre'}
# the starts anyone can choose (--origin, the Laser Prepare window): all of them since the centre
# job (written like LaserCut's) was confirmed on the machine (2026-10-21)
OFFERED_ORIGINS = ('top-left', 'top-right', 'bottom-left', 'bottom-right', 'center')


class ConversionError(Exception):
    pass


def nothing_found_message(skipped):
    hint = ''
    if skipped['text']:
        hint += f' Found {skipped["text"]} text object(s): select them and use Path > Object to Path.'
    if skipped['image']:
        hint += f' Found {skipped["image"]} image(s): photo engraving is not supported yet.'
    return 'Nothing to cut or engrave: no shapes with a fill or stroke colour.' + hint


def run_order(cuts, fills, preferred=()):
    """Order of (kind, colour) layers: all engraving first, then cuts with colours nested inside
    other colours' outlines first. `preferred` lists keys in the order the user gave them."""
    found = [('engrave', c) for c in fills] + [('cut', c) for c in cuts]
    order = [k for k in preferred if k in found] + [k for k in found if k not in preferred]
    if len(cuts) < 2:       # one cut colour: nesting can't change the order
        return sorted(order, key=lambda k: 0 if k[0] == 'engrave' else 1)
    closed = BoxIndex([(c, bbox(c)) for v in cuts.values() for c in v if len(c) > 3 and c[0] == c[-1]])

    def nesting(colour):
        return min(sum(1 for c, b in closed.near(p[len(p) // 2])
                       if c is not p and inside_box(p[len(p) // 2], c, b) and inside_box(p[0], c, b))
                   for p in cuts[colour])
    rank = {k: (0, 0) if k[0] == 'engrave' else (1, -nesting(k[1])) for k in order}
    return sorted(order, key=lambda k: rank[k])


def design_box(cuts, fills):
    """(x0, y0, x1, y1) in mm around every cut and engrave shape, or None if there are none."""
    xs = [x for v in cuts.values() for p in v for x, _ in p]
    xs += [x for v in fills.values() for rings, _ in v for r in rings for x, _ in r]
    ys = [y for v in cuts.values() for p in v for _, y in p]
    ys += [y for v in fills.values() for rings, _ in v for r in rings for _, y in r]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def origin_point(cuts, fills, origin='top-left'):
    """Where the head goes for `origin` (one of ORIGINS), in the design's mm coordinates (as
    loaded, +y down): a corner or the centre of the box of everything cut or engraved. Engrave
    overscan is not part of that box: it runs past the design's left and right edges whatever
    the origin."""
    box = design_box(cuts, fills)
    return molwriter.origin_point(box, origin) if box else None


class DesignJob(MolJob):
    """A MolJob whose box (header words 6-9, the start point, the Frame) also covers `design`
    (x0, y0, x1, y1 in steps): the shapes left out of the job keep their place, like LaserCut."""

    def __init__(self, layers, design, origin):
        super().__init__(layers, origin=origin)
        self.design = design

    def bbox(self):
        b, d = super().bbox(), self.design
        return min(b[0], d[0]), min(b[1], d[1]), max(b[2], d[2]), max(b[3], d[3])


def build_job(cuts, fills, settings, order, fallback=None, max_power=100.0, overcut=True,
              keep_order=False, scan=None, origin='top-left', design=None):
    """Build a MolJob. settings: {(kind, colour): {...}}; order: list of (kind, colour).
    origin: where the head is when the job starts, one of ORIGINS (see origin_point).
    design: (cuts, fills) of the whole drawing, shapes left out of the job included. The start
    point, the design size and the Frame are then worked out on all of it, as LaserCut does for
    objects not output: the shapes that run stay where they are relative to the head.

    Returns (job, report_lines). Raises ConversionError for bad settings. If `scan` is a dict,
    each engrave colour's scan lines (as from scanlines(), in mm) are put in it, for a preview.
    """
    fallback = fallback or {'cut': {}, 'engrave': {}}
    if origin not in ORIGINS:
        raise ConversionError(f'unknown origin {origin!r} (allowed: {", ".join(ORIGINS)})')
    box = design_box(cuts, fills)
    if not box:
        raise ConversionError('Nothing to cut or engrave.')
    whole = design_box(*design) if design else None
    if whole:
        box = tuple(f(a, b) for f, a, b in zip((min, min, max, max), box, whole))
    ox, oy = box[:2]
    report = [f'Design size: {box[2] - ox:.1f} x {box[3] - oy:.1f} mm']
    if origin != 'top-left':
        report.append(f"start: head at the design's {ORIGIN_NAMES[origin]}")
    S = STEPS_PER_MM
    hx, hy = molwriter.origin_point(box, origin)
    layers, pos = [], (hx - ox, hy - oy)
    for kind, colour in order:
        own = settings.get((kind, colour))
        s = {**DEFAULTS[kind], **(own if own is not None else fallback[kind])}
        note = '' if own is not None or fallback[kind] else \
            f'  (defaults; set with --{"layer" if kind == "cut" else "engrave"})'
        if kind == 'engrave':
            if not 0 <= s['power'] <= max_power:
                raise ConversionError(f'{colour}: power {s["power"]}% is outside 0..{max_power}%')
            shapes = fills[colour]
            lines, (bx0, by0, bx1, by1) = scanlines(shapes, s['interval'])
            if scan is not None:
                scan[colour] = lines
            step_lines = [(round((y - oy) * S), [(round((a - ox) * S), round((b - ox) * S)) for a, b in sp])
                          for y, sp in lines]
            bbox = (round((bx0 - ox) * S), round((by0 - oy) * S), round((bx1 - ox) * S), round((by1 - oy) * S))
            layer = EngraveLayer(step_lines, s['speed'], s['power'], s['power2'], s['overscan'],
                                 bool(s['bidir']), s['shift'], bbox=bbox,
                                 return_speed_mm_s=s['return'] or None)
            if layer.empty():
                continue
            layers.append(layer)
            pos = (layer.end()[0] / S, layer.end()[1] / S)
            report.append(f'engrave {colour}: {len(shapes)} shapes, {len(layer.lines)} lines '
                          f'@ {s["interval"]:g} mm, speed {s["speed"]:g} mm/s, power {s["power"]:g}%, '
                          f'{"both directions" if s["bidir"] else "one direction"}, '
                          f'overscan {layer.overscan / S:.1f} mm'
                          f'{"" if s["bidir"] else f", return {layer.return_speed / S:.0f} mm/s"}{note}')
            continue
        corner = s['corner'] if s['corner'] is not None else s['power']
        for value in (s['power'], corner):
            if not 0 <= value <= max_power:
                raise ConversionError(f'{colour}: power {value}% is outside 0..{max_power}%')
        paths = cuts[colour]
        shifted = [[(x - ox, y - oy) for x, y in p] for p in paths]
        if not keep_order:
            shifted = order_paths(shifted, pos)
        pos = shifted[-1][-1]
        steps = [to_steps(p) for p in shifted]
        layers.append(CutLayer(steps, s['speed'], s['power'], corner, overcut=overcut))
        report.append(f'cut {colour}: {len(paths)} shapes, speed {s["speed"]:g} mm/s, '
                      f'power {s["power"]:g}%, corner {corner:g}%{note}')
    if not layers:
        raise ConversionError('Nothing to cut or engrave.')
    if whole:
        steps_box = (0, 0, round((box[2] - ox) * S), round((box[3] - oy) * S))
        return DesignJob(layers, steps_box, origin), report
    return MolJob(layers, origin=origin), report


def skipped_notes(skipped):
    notes = []
    if skipped['text']:
        notes.append(f'skipped {skipped["text"]} text objects: in Inkscape use Path > Object to Path first')
    if skipped['image']:
        notes.append(f'skipped {skipped["image"]} images (photo engraving not supported yet)')
    return notes


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('svg')
    ap.add_argument('-o', '--output', default='LASER.MOL')
    ap.add_argument('--layer', dest='specs', action='append', type=spec_parser('cut'), default=[],
                    metavar='COLOUR:speed=S,power=P[,corner=C]', help='cut settings for a stroke colour')
    ap.add_argument('--engrave', dest='specs', action='append', type=spec_parser('engrave'),
                    metavar='COLOUR:speed=S,power=P[,interval=I,bidir=1]', help='engrave settings for a fill colour')
    ap.add_argument('--settings', help='JSON settings file (see above)')
    ap.add_argument('--default-cut', type=lambda t: spec_parser('cut')('x:' + t)[2], default={},
                    metavar='speed=S,power=P[,corner=C]', help='settings for stroke colours not listed elsewhere')
    ap.add_argument('--default-engrave', type=lambda t: spec_parser('engrave')('x:' + t)[2], default={},
                    metavar='speed=S,power=P[,...]', help='settings for fill colours not listed elsewhere')
    ap.add_argument('--no-overcut', action='store_true', help="don't add the 0.1 mm overcut on closed shapes")
    ap.add_argument('--keep-order', action='store_true', help='cut in document order (no travel optimisation)')
    ap.add_argument('--max-power', type=float, default=100.0, help='refuse power above this percent')
    ap.add_argument('--origin', choices=OFFERED_ORIGINS, default='top-left',
                    help="where the head is when the job starts: a corner or the centre of the design "
                         "(default top-left)")
    args = ap.parse_args(argv)

    specs = list(args.specs)
    if args.settings:
        with open(args.settings) as f:
            data = json.load(f)
        if 'cut' not in data and 'engrave' not in data:
            data = {'cut': data}
        for kind in ('engrave', 'cut'):
            for colour, s in data.get(kind, {}).items():
                specs.append((kind, Color(colour).hex, s))
    settings, preferred = {}, []
    for kind, colour, s in specs:
        if (kind, colour) not in settings:
            preferred.append((kind, colour))
        settings.setdefault((kind, colour), {}).update(s)

    cuts, fills, skipped = load(args.svg)
    if not cuts and not fills:
        sys.exit(nothing_found_message(skipped))
    order = run_order(cuts, fills, preferred)
    try:
        job, report = build_job(cuts, fills, settings, order,
                                {'cut': args.default_cut, 'engrave': args.default_engrave},
                                args.max_power, not args.no_overcut, args.keep_order,
                                origin=args.origin)
    except ConversionError as e:
        sys.exit(str(e))
    print(f'{args.svg}: {report[0].split(": ", 1)[1]}')
    for line in report[1:] + skipped_notes(skipped):
        print('  ' + line)
    job.write(args.output)
    print(f'wrote {args.output} ({os.path.getsize(args.output)} bytes, ~{job.estimate_seconds()} s)')


if __name__ == '__main__':
    main()
