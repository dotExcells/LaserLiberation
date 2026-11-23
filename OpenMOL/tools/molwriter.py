#!/usr/bin/env python3
"""Write Leetro LaserCut .MOL job files (vector cutting and filled-shape engraving).

Coordinates passed in are motor steps with +x to the right and +y DOWN (SVG
convention). The machine's Y axis points up, so y is negated when moves are
written. The job starts where the head is (immediate mode); MolJob's `origin`
says which point of the design that is (top-left by default).

See MOL_FORMAT.md for the format notes this is built from.
"""
import math
import struct

STEPS_PER_MM = 111.111111
Z_STEPS_PER_MM = 1600.0

# Motion constants used by LaserCut for this machine (steps/s, steps/s^2, steps/s^3).
START_SPEED = 555.0
RAPID_SPEED = 33333.0
RAPID_ACCEL = 55555.0
CUT_ACCEL = 38888.0
JERK = 22222222.0
SPEED_QUANTUM = 8 + 10 / 4096   # peak speeds of rapid profiles sit on this grid

FRAME_POWER = 4000        # 0.01 % units; laser-on frame in section 2
FRAME_SPEED = 2777.0
FRAME_MARGIN = 222.5      # steps added around the job for the laser-on frame
OVERCUT = 11              # steps LaserCut continues past the end of a closed path

AXES_XY = 0x304
BLOCK = 0x200

ENGRAVE_START_SPEED = 4444.0      # X speed at the ends of each scan pass
ENGRAVE_ACCEL = 666666.0
ENGRAVE_TERMINATOR = [0x00AE0002, 0x00250000, 0]
DUMMY_RUNS = [0x00250000, 0]      # laser data for a non-burning return pass
# LaserCut's "Acc space" (Machine Options): how far back the return passes of a both-direction
# engrave are shifted, by scan speed. (speeds below this mm/s, shift mm); LaserCut shows the
# shifts as negative (-0.20 mm up to 199 mm/s, ...): return-pass laser data move left (-x).
ACC_SPACE = ((200, 0.20), (400, 0.25), (600, 0.40), (math.inf, 0.35))
# counted in header word 4 (in the layer sections; 0x030500 = rapids between shapes)
MOVE_OPS = {0x020500, 0x014000, 0x040500, 0x030500}

ORIGINS = ('top-left', 'top-right', 'bottom-left', 'bottom-right', 'center')


def origin_point(box, origin):
    """Where the head is for `origin` (one of ORIGINS) on the design box (x0, y0, x1, y1), +y down."""
    x0, y0, x1, y1 = box
    if origin == 'center':
        return (x0 + x1) / 2, (y0 + y1) / 2
    if origin not in ORIGINS:
        raise ValueError(f'unknown origin {origin!r} (allowed: {", ".join(ORIGINS)})')
    return (x1 if origin.endswith('right') else x0), (y1 if origin.startswith('bottom') else y0)


def lfloat(value):
    """Encode a float in Leetro's format (IEEE single with exponent moved to the top byte)."""
    bits = struct.unpack('<I', struct.pack('<f', value))[0]
    sign = bits >> 31
    exp = ((bits >> 23) & 0xFF) - 127
    frac = bits & 0x7FFFFF
    return ((exp & 0xFF) << 24) | (sign << 23) | frac


def u32(v):
    return v & 0xFFFFFFFF


class Section:
    """A stream of records; tracks the top-level record count for the header."""

    def __init__(self):
        self.words = []
        self.count = 0
        self.moves = 0

    def rec(self, op, *args):
        args = [u32(a) for a in args]
        self.words += [(len(args) << 24) | op] + args
        self.count += 1
        self.moves += op in MOVE_OPS

    def var(self, op, args):
        args = [u32(a) for a in args]
        self.words += [0x80000000 | op, len(args)] + args
        self.count += 1


def nested(op, *args):
    return [(len(args) << 24) | op] + [u32(a) for a in args]


# ---------------------------------------------------------------- motion math

def seg_len(dx, dy):
    return int(round(math.hypot(dx, dy)))


def ramp_dist(v_from, v_to, accel):
    """Distance (steps) to change speed between v_from and v_to with jerk-limited accel."""
    lo, hi = sorted((v_from, v_to))
    return ((hi * hi - lo * lo) / accel + (hi + lo) * accel / JERK) / 2


def stop_profile(length, accel=RAPID_ACCEL, vmax=RAPID_SPEED):
    """Peak speed and decel distance for a move that starts and ends at START_SPEED."""
    lo, hi = 0, int((vmax - START_SPEED) / SPEED_QUANTUM)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if 2 * ramp_dist(START_SPEED, START_SPEED + mid * SPEED_QUANTUM, accel) <= length:
            lo = mid
        else:
            hi = mid - 1
    vpeak = START_SPEED + lo * SPEED_QUANTUM
    return vpeak, int(math.floor(ramp_dist(START_SPEED, vpeak, accel)))


def profile_args(vpeak, accel=RAPID_ACCEL, v_in=START_SPEED, v_out=START_SPEED):
    return [lfloat(v_in), lfloat(v_out), lfloat(vpeak), lfloat(accel), lfloat(JERK)]


# ---------------------------------------------------------------- job model

class CutLayer:
    def __init__(self, paths, speed_mm_s, power_pct, corner_power_pct=None, overcut=True):
        """paths: list of point lists [(x, y), ...] in steps, +y down."""
        self.paths = [[(int(round(x)), int(round(y))) for x, y in p] for p in paths if len(p) > 1]
        self.speed = float(int(speed_mm_s * STEPS_PER_MM))  # LaserCut truncates to whole steps/s
        self.power = int(round(power_pct * 100))
        cp = power_pct if corner_power_pct is None else corner_power_pct
        self.corner_power = int(round(cp * 100))
        self.overcut = overcut

    def empty(self):
        return not self.paths

    def bbox(self):
        xs = [x for p in self.paths for x, _ in p]
        ys = [y for p in self.paths for _, y in p]
        return min(xs), min(ys), max(xs), max(ys)

    def start(self):
        return self.paths[0][0]

    def end(self):
        p = self.paths[-1]
        return self._overcut_end(p) if self._closed(p) and self.overcut else p[-1]

    @staticmethod
    def _closed(p):
        return p[0] == p[-1]

    @staticmethod
    def _overcut_end(p):
        (x0, y0), (x1, y1) = p[0], p[1]
        d = math.hypot(x1 - x0, y1 - y0)
        return (x0 + int(round((x1 - x0) / d * OVERCUT)), y0 + int(round((y1 - y0) / d * OVERCUT)))


def _split_runs(on, off):
    """Data words are 16-bit on / 16-bit off; split runs longer than 65535 steps."""
    words = []
    while on > 0xFFFF:
        words.append([0xFFFF, 0])
        on -= 0xFFFF
    words.append([on, off])
    while words[-1][1] > 0xFFFF:
        extra = words[-1][1] - 0xFFFF
        words[-1][1] = 0xFFFF
        words.append([0, extra])
    return words


def laser_data(x_start, dx, spans, overscan):
    """Laser pattern record args for a scan pass from x_start moving dx (steps).

    spans: absolute [(x0, x1)] burn intervals (x1 exclusive) for this pass.
    """
    d = 1 if dx > 0 else -1
    r0 = x_start + d * overscan
    rel = sorted((a - r0, b - r0) if d > 0 else (r0 - b, r0 - a) for a, b in spans)
    region = abs(dx) - 2 * overscan
    n_blocks = region // 32 + 1
    if not rel:
        return [(n_blocks << 16) | len(DUMMY_RUNS)] + DUMMY_RUNS
    words, pos = [], 0
    if rel[0][0] > 0:
        words += _split_runs(0, rel[0][0])
        pos = rel[0][0]
    for i, (u0, u1) in enumerate(rel):
        nxt = rel[i + 1][0] if i + 1 < len(rel) else u1
        words += _split_runs(u1 - u0, nxt - u1)
        pos = nxt
    pad = 32 * n_blocks - pos
    if pad < 0 or rel[0][0] < 0:
        raise ValueError('burn spans fall outside the scan region')
    words[-1][1] += pad
    if words[-1][1] > 0xFFFF:
        last = words.pop()
        words += _split_runs(*last)
    return [(n_blocks << 16) | len(words)] + [(off << 16) | on for on, off in words]


def auto_overscan(speed):
    """Overscan (steps) long enough to reach scan speed before the burn, with margin.

    Uses the engrave acceleration profile: 20 % extra plus 1 mm. At 400 mm/s this gives
    ~25 mm, the value LaserCut used; at 100 mm/s about 4.4 mm.
    """
    ramp = ramp_dist(ENGRAVE_START_SPEED, max(speed, ENGRAVE_START_SPEED), ENGRAVE_ACCEL)
    return int(math.ceil(ramp * 1.2 + STEPS_PER_MM))


def acc_space(speed_mm_s):
    """The return-pass shift (mm, left) LaserCut uses for a both-direction engrave at this speed."""
    return next(mm for below, mm in ACC_SPACE if speed_mm_s < below)


class EngraveLayer:
    def __init__(self, lines, speed_mm_s, power_pct, power2_pct=60.0, overscan_mm=25.0,
                 bidirectional=False, bidir_shift_mm=None, bbox=None, return_speed_mm_s=None):
        """lines: [(y, [(x0, x1), ...]), ...] in steps, +y down, x1 exclusive.

        overscan_mm=None picks the overscan from the scan speed (see auto_overscan).
        bidir_shift_mm moves the return passes left in both-direction mode; None picks it from
        the scan speed like LaserCut (see acc_space).
        return_speed_mm_s sets the speed of the laser-off return passes in one-direction mode;
        None runs them at the scan speed like LaserCut.
        Lines are engraved bottom to top like LaserCut. bbox (x0, y0, x1, y1) is the design's
        outline for the frame/header; it defaults to the extent of the lines.
        """
        self.lines = sorted(((int(y), sorted((int(a), int(b)) for a, b in spans if b > a))
                             for y, spans in lines), key=lambda l: -l[0])
        self.lines = [l for l in self.lines if l[1]]
        self.speed = float(int(speed_mm_s * STEPS_PER_MM))
        self.power = int(round(power_pct * 100))
        self.power2 = int(round(power2_pct * 100))
        self.overscan = (auto_overscan(self.speed) if overscan_mm is None
                         else int(overscan_mm * STEPS_PER_MM))
        self.return_speed = (self.speed if return_speed_mm_s is None
                             else float(int(max(return_speed_mm_s, speed_mm_s) * STEPS_PER_MM)))
        self.bidirectional = bidirectional
        if bidir_shift_mm is None:
            bidir_shift_mm = acc_space(speed_mm_s)
        self.shift = int(round(bidir_shift_mm * STEPS_PER_MM))
        self._bbox = bbox
        self._passes = self._plan() if self.lines else []

    def empty(self):
        return not self.lines

    def bbox(self):
        if self._bbox:
            return self._bbox
        xs = [x for _, spans in self.lines for span in spans for x in span]
        ys = [y for y, _ in self.lines]
        return min(xs), min(ys), max(xs), max(ys)

    def _plan(self):
        """Lay out scan passes: [(x_start, y, dx, spans, burns)], plus start/end points."""
        over, passes = self.overscan, []
        lines = self.lines
        x = lines[0][1][0][0] - over
        self._start = (x, lines[0][0])
        if not self.bidirectional:
            for k, (y, spans) in enumerate(lines):
                s, e = spans[0][0], spans[-1][1]
                x = min(x, s - over)
                end = e + over + 1
                passes.append((x, y, end - x, spans, True))
                back = x
                if k + 1 < len(lines):
                    back = min(x, lines[k + 1][1][0][0] - over)
                passes.append((end, y, back - end, [], False))
                x = back
        else:
            shifted = lambda sp: [(a - self.shift, b - self.shift) for a, b in sp]  # noqa: E731
            for k, (y, spans) in enumerate(lines):
                nxt = lines[k + 1][1] if k + 1 < len(lines) else None
                if k % 2 == 0:
                    end = spans[-1][1] + over + 1
                    if nxt:
                        end = max(end, nxt[-1][1] - self.shift + over + 1)
                    passes.append((x, y, end - x, spans, True))
                else:
                    sp = shifted(spans)
                    end = sp[0][0] - over
                    if nxt:
                        end = min(end, nxt[0][0] - over)
                    passes.append((x, y, end - x, sp, True))
                x = end
        x_end, y_end = passes[-1][0] + passes[-1][2], passes[-1][1]
        self._end = (x_end, y_end)
        return passes

    def start(self):
        return self._start

    def end(self):
        return self._end

    def section(self, sid):
        s = Section()
        speed = lfloat(self.speed)
        s.rec(0x300C0B, sid)
        s.rec(0x000420, 3000)
        s.rec(0x000220, 2)
        s.rec(0x000620, self.overscan)
        s.rec(0x000520, 0)
        s.rec(0x014301, 4, speed)
        s.rec(0x010001, 3, lfloat(START_SPEED))
        s.rec(0x002009, 0, 10, lfloat(0.0), lfloat(10000.0), 1)
        s.rec(0x000120, self.power)
        s.rec(0x002009, 0, 10, 0, 10, lfloat(0.0), lfloat(10000.0), 1)
        s.rec(0x000120, self.power, self.power2)
        s.rec(0x000105, 256)
        profile = [4, lfloat(ENGRAVE_START_SPEED), speed, lfloat(ENGRAVE_ACCEL), lfloat(JERK)]
        back = [4, lfloat(ENGRAVE_START_SPEED), lfloat(self.return_speed), lfloat(ENGRAVE_ACCEL), lfloat(JERK)]
        last_burn = max(i for i, p in enumerate(self._passes) if p[4])
        for i, (x, y, dx, spans, burns) in enumerate(self._passes):
            if i > 0 and y != self._passes[i - 1][1]:
                s.rec(0x040500, 3, self._passes[i - 1][1] - y)
            s.var(0x000720, laser_data(x, dx, spans, self.overscan))
            s.rec(0x000001, *(profile if burns else back))
            s.rec(0x014000, 4, dx)
            if i == last_burn:
                s.rec(0x000105, 0)
        s.var(0x000720, ENGRAVE_TERMINATOR)
        s.rec(0x400C0B, sid)
        return s

    def seconds(self):
        # travel at scan/return speed plus LaserCut's allowance for speeding up on every pass
        t = 0.0
        for p in self._passes:
            v = self.speed if p[4] else self.return_speed
            t += abs(p[2]) / v + v / ENGRAVE_ACCEL
        return t


class MolJob:
    def __init__(self, layers, canvas_origin=(29852, 69833), origin='top-left'):
        """canvas_origin: LaserCut-style absolute position of the head (steps). Only used for the
        SUB603 parameters; immediate mode runs relative to the head.
        origin: where the head is when the job starts: one of ORIGINS (on the box of all layers'
        designs, engrave overscan not included) or an (x, y) point in the layers' steps."""
        self.layers = [l for l in layers if not l.empty()]
        self.canvas_origin = canvas_origin
        self.origin = origin

    def head(self):
        """The head's start position in the layers' coordinates (steps, +y down)."""
        if isinstance(self.origin, str):
            return self._point(self.origin)
        return tuple(self.origin)

    def _point(self, name):
        x, y = origin_point(self.bbox(), name)
        if name == 'center':
            # LaserCut rounds the centre half up in its canvas coordinates (+y up)
            return int(math.floor(x + 0.5)), -int(math.floor(0.5 - y))
        return int(math.floor(x)), int(math.floor(y))

    @staticmethod
    def corners(box):
        """box's corners in the order the Frame traces them: down, right, up, left from the top-left."""
        x0, y0, x1, y1 = box
        return [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]

    def frame_start(self):
        """Index in corners() of the corner the Frame starts from (the head's, or one within a step
        of it: LaserCut's canvas rounding), None if the head is not on a corner (then it starts at
        the top-left)."""
        hx, hy = self.head()
        for i, (cx, cy) in enumerate(self.corners(self.bbox())):
            if abs(cx - hx) <= 1 and abs(cy - hy) <= 1:
                return i
        return None

    # bounding box of all layers' designs (steps, +y down)
    def bbox(self):
        boxes = [l.bbox() for l in self.layers]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def sub603_args(self):
        ox, oy = self.canvas_origin
        return [603, lfloat(START_SPEED), lfloat(RAPID_SPEED), lfloat(RAPID_ACCEL), lfloat(JERK),
                lfloat(ox), lfloat(oy)]

    # ------------------------------------------------------------ sections

    def main_section(self):
        s = Section()
        s.rec(0x200D0B)
        s.var(0x600101, [4, lfloat(4.0)])
        s.var(0x600101, [3, lfloat(3.0)])
        s.rec(0x200801)
        s.rec(0x200701)
        s.var(0x600101, self.sub603_args() + [lfloat(0.0), lfloat(0.0)])
        s.rec(0x015001, 4, lfloat(STEPS_PER_MM))
        s.rec(0x015001, 3, lfloat(STEPS_PER_MM))
        s.rec(0x015001, 2, lfloat(Z_STEPS_PER_MM))
        s.rec(0x200901, 1)
        pos = self.head()
        for i, layer in enumerate(self.layers):
            self._rapid(s, pos, layer.start())
            s.var(0x50000B, [3 + i, lfloat(0.0), lfloat(0.0)])
            pos = layer.end()
        s.rec(0x000608, 0)
        s.var(0x50000B, [603])
        s.rec(0x200E0B)
        return s

    def frame_section(self):
        """Section 1: trace the job outline with the laser off (panel Frame button).

        Like LaserCut, the trace starts at the head's corner (a head a step off it moves there
        first). A head off the corners (centre) goes to the top-left corner first. The trace ends
        at its first corner: no move back to the head (the controller brings it back itself)."""
        k, head = self.frame_start(), self.head()
        ring = self.corners(self.bbox())
        ring = ring[k or 0:] + ring[:k or 0]
        pts = [head] + ring + ring[:1]
        if pts[0] == pts[1]:
            pts = pts[1:]
        s = Section()
        s.rec(0x300C0B, 1)
        s.var(0x600101, self.sub603_args())
        legs = [pts[i:i + 2] for i in range(len(pts) - 1)]
        if k is not None and len(legs) > 4:
            legs[:2] = [pts[:3]]    # the step onto the corner shares the first edge's profile
        body = []
        for leg in legs:
            body += self._stop_move(*leg)
        s.var(0x00080B, body)
        s.rec(0x400C0B, 1)
        s.frame_moves = len(pts) - 1
        return s

    def frame_cut_section(self):
        """Section 2: laser-on frame FRAME_MARGIN outside the job (SUB610).

        Starts at the frame corner beyond the head's corner (the top-left one for a head off the
        corners) and ends there, like LaserCut: no move back to the head."""
        x0, y0, x1, y1 = self.bbox()
        left = int(math.ceil(FRAME_MARGIN))
        top = int(math.floor(FRAME_MARGIN))
        k, head = self.frame_start(), self.head()
        ring = self.corners((x0 - left, y0 - top, x1 + left - 1, y1 + top + 1))
        ring = ring[k or 0:] + ring[:k or 0]
        s = Section()
        s.rec(0x300C0B, 2)
        s.var(0x600101, self.sub603_args())
        s.rec(0x002009, FRAME_POWER, FRAME_POWER, lfloat(START_SPEED), lfloat(FRAME_SPEED), 0)
        s.rec(0x002009, FRAME_POWER, FRAME_POWER, FRAME_POWER, FRAME_POWER,
              lfloat(START_SPEED), lfloat(FRAME_SPEED), 0)
        # move out to the frame corner
        body = self._stop_move(head, ring[0])
        body += nested(0x000220, 1)
        s.var(0x00080B, body)
        # trace the frame with the laser on
        pts = ring + ring[:1]
        edges = [(bx - ax, -(by - ay)) for (ax, ay), (bx, by) in zip(pts, pts[1:])]
        total = sum(seg_len(*e) for e in edges)
        vpeak = FRAME_SPEED * 0.8
        decel = int(math.floor(ramp_dist(START_SPEED, vpeak, CUT_ACCEL)))
        body = nested(0x000101, *profile_args(vpeak, CUT_ACCEL))
        body += nested(0x03000C, total, decel)
        for e in edges:
            body += nested(0x020500, e[0], e[1], seg_len(*e))
        body += nested(0x000220, 0)
        s.var(0x00080B, body)
        s.rec(0x400C0B, 2)
        return s

    @staticmethod
    def _stop_move(*pts):
        """Nested records for a laser-off move through pts (+y down) under one speed profile that
        starts and ends slow."""
        moves = [(b[0] - a[0], -(b[1] - a[1])) for a, b in zip(pts, pts[1:])]
        lengths = [seg_len(dx, dy) for dx, dy in moves]
        vpeak, decel = stop_profile(sum(lengths))
        body = nested(0x000101, *profile_args(vpeak)) + nested(0x03000C, sum(lengths), decel)
        for (dx, dy), length in zip(moves, lengths):
            body += nested(0x020500, dx, dy, length)
        return body

    def cut_section(self, layer, sid):
        s = Section()
        speed = lfloat(layer.speed)
        s.rec(0x300C0B, sid)
        s.rec(0x000420, 3000)
        s.rec(0x002009, layer.corner_power, layer.power, lfloat(START_SPEED), speed, 1)
        s.rec(0x002009, 0, 10, lfloat(0.0), lfloat(10000.0), 1)
        s.rec(0x000120, layer.power)
        s.rec(0x002009, layer.corner_power, layer.power, 0, 0, lfloat(START_SPEED), speed, 1)
        s.rec(0x002009, 0, 10, 0, 10, lfloat(0.0), lfloat(10000.0), 1)
        s.rec(0x000120, layer.power, 0)
        s.rec(0x000105, 256)
        s.rec(0x000608, 2000)
        pos = None
        for path in layer.paths:
            if pos is not None:
                s.rec(0x000220, 0)
                self._rapid(s, pos, path[0])
            s.rec(0x000220, 1)
            s.rec(0x000201, lfloat(RAPID_SPEED))
            s.rec(0x000201, speed)
            pts = list(path)
            if layer.overcut and CutLayer._closed(path):
                pts.append(CutLayer._overcut_end(path))
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                dx, dy = bx - ax, -(by - ay)
                if dx or dy:
                    s.rec(0x020500, AXES_XY, dx, dy, seg_len(dx, dy))
            pos = pts[-1]
        s.rec(0x000220, 0)
        s.rec(0x000105, 0)
        s.rec(0x000105, 0)
        s.rec(0x000608, 2000)
        s.rec(0x400C0B, sid)
        return s

    @staticmethod
    def _rapid(s, a, b):
        dx, dy = b[0] - a[0], -(b[1] - a[1])
        if not (dx or dy):
            return
        length = seg_len(dx, dy)
        vpeak, decel = stop_profile(length)
        s.rec(0x000101, *profile_args(vpeak))
        s.rec(0x030500, AXES_XY, dx, dy, length, decel)

    # ------------------------------------------------------------ output

    def estimate_seconds(self):
        t = 0.0
        for layer in self.layers:
            if isinstance(layer, EngraveLayer):
                t += layer.seconds()
                continue
            for p in layer.paths:
                t += sum(math.dist(a, b) for a, b in zip(p, p[1:])) / layer.speed
        return int(t)

    def layer_section(self, layer, sid):
        if isinstance(layer, EngraveLayer):
            return layer.section(sid)
        return self.cut_section(layer, sid)

    def to_bytes(self):
        sections = [self.main_section(), self.frame_section(), self.frame_cut_section()]
        layer_secs = [self.layer_section(l, 3 + i) for i, l in enumerate(self.layers)]
        sections += layer_secs
        blocks, body, block = [], b'', 1
        for sec in sections:
            raw = struct.pack(f'<{len(sec.words)}I', *sec.words)
            raw += b'\0' * (-len(raw) % BLOCK)
            blocks.append(block)
            block += len(raw) // BLOCK
            body += raw
        # moves in the layer sections + moves in Frame section 1
        moves = sum(sec.moves for sec in layer_secs) + sections[1].frame_moves
        x0, y0, x1, y1 = self.bbox()
        hx, hy = self.head()
        header = [0] * (BLOCK // 4)
        header[0] = BLOCK + len(body)
        header[1] = sum(sec.count for sec in sections)
        header[2] = 0x04030000
        header[3] = 1
        header[4] = moves
        # flags; 0x100 = job relative to the head (the machine's "relative to selection" mode),
        # without it the machine may run the job in machine coordinates. Same for every start.
        header[5] = 0x101
        # the job box's corners relative to the head, machine +y up: (right, top), (left, bottom)
        header[6], header[7] = u32(x1 - hx), u32(hy - y0)
        header[8], header[9] = u32(x0 - hx), u32(hy - y1)
        header[10] = 0x00551746
        header[11] = 0x224F0000
        header[12] = self.estimate_seconds()
        for i, b in enumerate(blocks):
            header[0x70 // 4 + i] = b
        return struct.pack(f'<{len(header)}I', *header) + body

    def write(self, path):
        with open(path, 'wb') as f:
            f.write(self.to_bytes())
