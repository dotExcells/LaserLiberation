#!/usr/bin/env python3
"""Write the shapes Laser Prepare works with back out as a drawing (SVG or DXF), e.g. after
recolouring shapes in the window, so the file is set up for next time.

The copy holds what the laser tools see: outlines (cuts) as polylines in their stroke colour and
filled areas (engraving) as filled shapes in their fill colour, in mm. Curves are the 0.1 mm
segments they were flattened to; text and images, which the tools skip, are not in it.
Reading the copy back (svg2mol.load / dxf_load.load) gives the same shapes and colours.
"""
import math


def _num(v):
    return f'{v:.4f}'.rstrip('0').rstrip('.')


def _closed(p):
    return len(p) > 2 and math.dist(p[0], p[-1]) < 1e-9


def bounds(cuts, fills):
    pts = [q for v in cuts.values() for p in v for q in p]
    pts += [q for v in fills.values() for rings, _ in v for r in rings for q in r]
    return (min(x for x, _ in pts), min(y for _, y in pts),
            max(x for x, _ in pts), max(y for _, y in pts))


def save_svg(path, cuts, fills):
    """cuts {colour: [polyline]}, fills {colour: [(rings, rule)]}, mm with +y down."""
    _, _, x1, y1 = bounds(cuts, fills)
    # the page starts at 0,0 with 1 unit = 1 mm, so coordinates read back exactly as written
    w, h = max(x1, 0.001), max(y1, 0.001)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(w)}mm" height="{_num(h)}mm" '
           f'viewBox="0 0 {_num(w)} {_num(h)}">',
           '<!-- Written by Laser Prepare: fill colour = engrave, outline colour = cut -->']

    def d(poly, close):
        pts = poly[:-1] if close else poly
        return 'M' + ' L'.join(f'{_num(x)},{_num(y)}' for x, y in pts) + (' Z' if close else '')
    for colour, shapes in fills.items():
        for rings, rule in shapes:
            out.append(f'<path d="{" ".join(d(r, True) for r in rings)}" fill="{colour}" '
                       f'fill-rule="{rule}" stroke="none"/>')
    for colour, polys in cuts.items():
        for p in polys:
            out.append(f'<path d="{d(p, _closed(p))}" fill="none" stroke="{colour}" stroke-width="0.1"/>')
    out.append('</svg>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')


def save_dxf(path, cuts, fills):
    """The same as DXF (mm; DXF +y is up, so y is flipped back like dxf_load does when reading).
    Each colour gets a layer with that colour; fills become HATCH areas (even-odd)."""
    import ezdxf
    doc = ezdxf.new('R2010', setup=False)
    doc.header['$INSUNITS'] = 4
    doc.header['$MEASUREMENT'] = 1
    msp = doc.modelspace()
    layers = {}

    def layer(kind, colour):
        name = f'{kind}_{colour.lstrip("#").upper()}'
        if name not in layers:
            doc.layers.add(name, color=7).rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
            layers[name] = True
        return name

    def pts(p):
        return [(x, -y) for x, y in p]
    for colour, shapes in fills.items():
        name = layer('ENGRAVE', colour)
        for rings, _rule in shapes:
            hatch = msp.add_hatch(color=256, dxfattribs={'layer': name})   # 256 = colour by layer
            hatch.dxf.hatch_style = 0
            for r in rings:
                hatch.paths.add_polyline_path(pts(r[:-1] if _closed(r) else r), is_closed=True)
    for colour, polys in cuts.items():
        name = layer('CUT', colour)
        for p in polys:
            c = _closed(p)
            msp.add_lwpolyline(pts(p[:-1] if c else p), close=c, dxfattribs={'layer': name})
    doc.saveas(path)


def save(path, cuts, fills):
    if path.lower().endswith('.dxf'):
        save_dxf(path, cuts, fills)
    else:
        save_svg(path, cuts, fills)
