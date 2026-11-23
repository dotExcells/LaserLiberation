#!/usr/bin/env python3
"""Read a DXF (e.g. from Fusion, FreeCAD, LibreCAD) into the same shapes svg2mol.load() returns.

Lines, arcs, circles, ellipses, splines and polylines become CUT paths grouped by their colour
(ByLayer/ByBlock resolved). HATCH and SOLID areas become ENGRAVE fills. Blocks (INSERTs) are
expanded. Units come from the drawing's $INSUNITS (unitless drawings are taken as mm). DXF's
+y up is flipped to +y down like SVG, so the design's top-left stays the job's start point.
"""
import math

try:
    import ezdxf
    from ezdxf import colors, disassemble, path as ezpath
except ImportError:  # reported by load() so SVG-only use still works
    ezdxf = None

from svg2mol import SEGMENT_MM

# $INSUNITS code -> millimetres per drawing unit
UNIT_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254,
           10: 914.4, 14: 100.0}
CUT_TYPES = {'LINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE', 'LWPOLYLINE', 'POLYLINE', 'HELIX'}
JOIN_MM = 0.01                # ends closer than this are treated as touching
FILL_TYPES = {'HATCH', 'SOLID', 'TRACE', '3DFACE'}


def colour_hex(entity, layer_colours):
    """Resolve an entity's display colour as '#rrggbb'. Black/white (7) is shown as black."""
    if entity.dxf.hasattr('true_color'):
        r, g, b = colors.int2rgb(entity.dxf.true_color)
    else:
        aci = entity.dxf.get('color', colors.BYLAYER)
        if aci in (colors.BYLAYER, colors.BYBLOCK):
            layer = entity.dxf.get('layer', '0')
            aci, true = layer_colours.get(layer, (7, None))
            if true is not None:
                return '#%02x%02x%02x' % true
        aci = abs(aci) or 7
        if aci == 7:
            return '#000000'
        r, g, b = colors.aci2rgb(aci)
    return '#%02x%02x%02x' % (r, g, b)


def load(dxf_file):
    """Return cuts {colour: [polyline]}, fills {colour: [(rings, rule)]}, skipped counts."""
    if ezdxf is None:
        raise RuntimeError('ezdxf is missing: run `.venv/bin/pip install ezdxf`.')
    doc = ezdxf.readfile(dxf_file)
    scale = UNIT_MM.get(doc.header.get('$INSUNITS', 0), 1.0)
    distance = SEGMENT_MM / 10 / scale     # max 0.01 mm off the true curve, in drawing units

    layer_colours, hidden = {}, set()
    for layer in doc.layers:
        true = colors.int2rgb(layer.dxf.true_color) if layer.dxf.hasattr('true_color') else None
        layer_colours[layer.dxf.name] = (abs(layer.dxf.get('color', 7)) or 7, true)
        if layer.is_off() or layer.is_frozen():
            hidden.add(layer.dxf.name)

    def pt(v):
        return (v.x * scale, -v.y * scale)

    cuts, fills, skipped = {}, {}, {'text': 0, 'image': 0}
    for e in disassemble.recursive_decompose(doc.modelspace()):
        kind = e.dxftype()
        if e.dxf.get('layer', '0') in hidden or e.dxf.get('invisible', 0):
            continue
        if kind in ('TEXT', 'MTEXT', 'ATTRIB'):
            skipped['text'] += 1
            continue
        if kind in ('IMAGE', 'WIPEOUT'):
            skipped['image'] += 1
            continue
        if kind not in CUT_TYPES and kind not in FILL_TYPES:
            continue
        colour = colour_hex(e, layer_colours)
        try:
            p = ezpath.make_path(e)
        except Exception:
            continue
        if kind in FILL_TYPES:
            rings = []
            for sub in p.sub_paths():
                ring = [pt(v) for v in sub.flattening(distance)]
                if len(ring) > 2:
                    if ring[0] != ring[-1]:
                        ring.append(ring[0])
                    rings.append(ring)
            if rings:
                fills.setdefault(colour, []).append((rings, 'evenodd'))
            continue
        for sub in p.sub_paths():
            poly = [pt(v) for v in sub.flattening(distance)]
            if len(poly) < 2:
                continue
            if math.dist(poly[0], poly[-1]) < JOIN_MM:
                poly[-1] = poly[0]
            elif sub.is_closed:
                poly.append(poly[0])
            cuts.setdefault(colour, []).append(poly)
    cuts = {c: join_paths(v) for c, v in cuts.items()}
    return cuts, fills, skipped


def join_paths(paths):
    """Chain open paths whose ends touch (CAD exports often draw a rectangle as four LINEs),
    so outlines become closed shapes for inside-first cutting and engraving."""
    done = [p for p in paths if len(p) > 2 and p[0] == p[-1]]
    open_ = [list(p) for p in paths if not (len(p) > 2 and p[0] == p[-1])]

    def key(q):
        return (round(q[0] / JOIN_MM), round(q[1] / JOIN_MM))
    # open paths by end key, so a growing chain finds its neighbours without scanning them all
    alive = [True] * len(open_)
    ends = {}
    for i, p in enumerate(open_):
        for k in {key(p[0]), key(p[-1])}:
            ends.setdefault(k, []).append(i)
    last = len(open_) - 1
    while True:
        while last >= 0 and not alive[last]:
            last -= 1
        if last < 0:
            break
        chain = open_[last]
        alive[last] = False
        while not (len(chain) > 2 and key(chain[0]) == key(chain[-1])):
            head, tail = key(chain[0]), key(chain[-1])
            found = [i for k in {head, tail} for i in ends.get(k, ()) if alive[i]]
            if not found:
                break
            i = min(found)          # the first touching path in drawing order
            p = open_[i]
            if key(p[0]) == tail:
                chain += p[1:]
            elif key(p[-1]) == tail:
                chain += p[-2::-1]
            elif key(p[-1]) == head:
                chain[:0] = p[:-1]
            else:
                chain[:0] = p[:0:-1]
            alive[i] = False
        if len(chain) > 2 and key(chain[0]) == key(chain[-1]):
            chain[-1] = chain[0]
        done.append(chain)
    return done
