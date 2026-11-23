# Leetro LaserCut `.MOL` format — decoding notes

Reverse-engineered from `LASER.MOL` (with its text dump `LASER.TXT`) and the LaserCut samples
in `laser decode/` (A–L and M_origins, each a `LASER.MOL` + `LASER.TXT` made on the old PC, see
"Samples" at the end). The `.TXT` files are LaserCut's own readable dump of the same job; the
`CMDxxx` names in them are LaserCut's, the opcodes below are what's in the binary. Other sources,
for facts only: the London Hackspace wiki
(wiki.london.hackspace.org.uk/view/Project:RELaserSoftware/MOL_file_format) and the samples in
github.com/parogers/mol-cnc-format (an older LaserCut, see "Other LaserCut versions").

Status: cutting and filled-shape engraving are understood well enough to generate files that the
machine runs (confirmed on the machine 2026-10-15). `tools/test_roundtrip.py` rebuilds every
sample; differences that remain are listed under "Known approximations".

## Basics

- Little-endian 32-bit words. Units are motor steps (111.111 steps/mm on X/Y here).
- **Record** = opcode word + args.
  - bit 31 clear → top byte of the opcode is the arg count.
  - bit 31 set → next word is the arg count (variable length).
- **Leetro float**: top byte = signed exponent `e`, bit 23 = sign, bits 0–22 = fraction:
  `value = ±(1 + frac/2^23) · 2^e`. This is exactly an IEEE single with the exponent
  (unbiased, `e − 127`) moved to the top byte and the sign moved to bit 23, so 0.0 encodes as
  `0x81000000`. `molwriter.lfloat()` does the conversion via `struct`.
- Powers are integers in 0.01 % (`5000` = 50 %). Speeds are floats in steps/s, **truncated to
  whole steps/s** (10 mm/s → `1111`, not 1111.11).
- Axis numbers in records: 4 = X, 3 = Y, 2 = Z/U (the 1600 steps/mm axis). `0x304` is the
  X+Y axis mask used by vector moves.

## Header (0x000–0x1FF)

| Word | Meaning | Example (square) |
|---|---|---|
| 0 | file size in bytes | 2560 |
| 1 | total record count in the file | 50 |
| 2 | version `0x04030000` | |
| 3 | 1 | |
| 4 | move count: moves in the layer sections + moves in Frame section 1 (display only, below) | 9 |
| 5 | flags: `0x101` = job relative to the head (always ours), `0x1` = not relative (see below) | |
| 6, 7 | the job box's right, top edge (x, y; machine +y is up), relative to the head | 2222, 0 |
| 8, 9 | its left, bottom edge | 0, −2222 |
| 10, 11 | `0x00551746`, `0x224F0000` (the TXT's SET200): constant per LaserCut installation (other setups have other values); copied unchanged | |
| 12 | estimated job time, seconds | 8 |
| 0x70.. | section table: the **512-byte block number** where each section starts, in section order (main = 1, frame = 2, laser frame = 3, layers 4, 5, …) | `1,2,3,4` |

With the usual top-left start, words 6 and 9 are the width and −height. Jobs placed on the bed
(box hundreds of mm from the head, first rapid move goes there) also exist in old files.
`mol2dxf.py` reports which start a job has.

**Word 5** is four flag bytes (London Hackspace wiki: "four booleans"; the second, `0x100`, "makes
artwork relative to the user origin"). Settled by the M_origins samples: `0x101` in every relative
job whatever the start (top-left, top-right, centre, centre top; also A–L, the root `LASER.MOL` and
the parogers samples), and `0x1` in the one made in LaserCut's non-relative mode. The main section's
`00200901` (TXT `CMD110`) goes with it: `[1]` relative, `[0]` not. Nothing else differs: the moves
are still relative, so the machine presumably first goes to the SUB603 position on the bed and
runs the job from there. The user always runs relative (the head's position is the start), and our
writer always writes `0x101` and `00200901 [1]`. The old LaserCut centre job with `1` was a
non-relative job.

**Start positions as LaserCut writes them** (M_origins: the same 20 mm square, one cut layer):

| Start | words 6–9 | first rapid | SUB603 |
|---|---|---|---|
| top-left | 2223, 0, 0, −2222 | +2223, −2222 | the design's top-left |
| top-right | 0, −1, −2223, −2223 | −2223, −2223 | top-right |
| centre | 1111, 1110, −1112, −1112 | +1111, −1112 | the box centre |
| centre top (middle of the top edge) | 1111, −1, −1112, −2223 | +1111, −2223 | middle of the top edge |

Words 6–9 are always the box relative to the head, and the first rapid always goes from the head to
the first layer's start; the start position shows nowhere else in the main and layer sections.
There is no return move at the end of the job for any start (main ends with `00000608 [0]` and the
call to 603). The ±1 values (`−1`, `−2223`) are canvas rounding: the design's top edge lies about
half a step off the grid, the box words round it down and the head position rounds it up (in all
but the top-left sample). With a top-right start LaserCut also starts the square at a different
corner (bottom-left instead of bottom-right; it picks each shape's start and direction itself,
"Automation set cut director" in its options). Frame sections: see there.

LaserCut's origin is a point on its canvas (`LogicOrgPositionX/Y` in its `.INI`); in the
`.MOL` it shows only as this box, the SUB603 canvas position (see below) and the moves.

**Start position (`MolJob(origin=…)`, `svg2mol.py --origin`)**: top-left (default, byte-identical to
before), top-right, bottom-left or bottom-right corner of the box of everything cut or engraved
(engrave overscan not included: it always runs past the design's left and right edges, so the head
needs that room beside the design whatever the start). The first rapid goes from the head to the
first layer's start; words 6–9 hold the box relative to the head (the start position is read back
from this box, never from word 5); word 5 is `0x101` for every start. The Frame sections are
written like LaserCut's (below): from the head's corner, or from the top-left for a head off the
corners, with no move back to the head. A head within 1 step of a corner counts as on it (with
LaserCut's 1-step move first). Rebuilt with each M_origins sample's head (`test_roundtrip.py`),
top-left, top-right, centre and centre top match LaserCut section by section apart from the known
approximations. The centre is built (`MolJob(origin='center')`: the box centre rounded half up in
+y-up coordinates, as LaserCut does; LaserCut's head can still be 1 step off in y because its
design lies between steps). Our centre job (a Laser Prepare test job) was confirmed on the
machine on 2026-10-21 and the centre is offered (`--origin center`, Laser Prepare).

Bytes after the used header are zero. **Word 4** = the number of `00020500`, `00014000`,
`00040500` and `00030500` (rapids between shapes) records in the layer sections, + the number of
moves in Frame section 1 (4 for a head on a corner; 5 for the centre, centre top, or a head 1 step
off the corner). It matches every sample except the original big `LASER.MOL` (5010 vs 9007, made
by a different LaserCut setup); the `00030500` count comes from a LaserCut "cut 5 times" sample
(not in the repo: 33 = 25 cuts + 4 rapids + 4 frame moves). Our writer counts the same; before
2026-10-21 it added a fixed 4 and left out `00030500`, which the machine accepted, so it is
probably just the progress display. Words 4 and 12 are display values; the
machine has run files where they were approximate.

## Sections (each starts on a 0x200 boundary)

- **0x200 main** (`0x00200D0B` … `0x00200E0B`): setup, then one rapid move + subroutine
  call per layer:
  - `80600101 [4, f4.0]`, `80600101 [3, f3.0]`, `00200801`, `00200701`
  - `80600101 [603, f555, f33333, f55555, f22222222, fX, fY, 0.0, 0.0]`: SUB603 params (start
    speed, rapid speed, accel, jerk, then the head's start position in LaserCut's canvas steps:
    the start point, e.g. the design's top-left for a top-left start). The canvas position
    doesn't matter in relative mode; `MolJob` always uses a sample's (29852, 69833).
  - `00015001 [4, f111.111]`, `[3, f111.111]`, `[2, f1600]` — steps/mm per axis
  - `00200901 [1]`: relative to the head (`[0]` in LaserCut's non-relative mode, see word 5)
  - per layer: `00000101 [f555, f555, fVpeak, f55555, f22222222]` +
    `00030500 [0x304, dx, dy, L, h]` (rapid to layer start) + `8050000B [layer_id, 0.0, 0.0]` (call)
  - `00000608 [0]`, `8050000B [603]`
- **0x400 section 1, 0x600 section 2**: frame / preview routines (SUB609/SUB610), used by the
  panel's Frame functions, not by a normal run. See "Frame sections".
- **0x800, 0xA00, … layer sections** `00300C0B [id]` … `00400C0B [id]`, ids 3, 4, …

### Vector cut layer section

```
00300C0B [id]
00000420 [3000]
00002009 [cornerPwr, maxPwr, f555, fCutSpeed, 1]
00002009 [0, 10, 0.0, f10000, 1]
00000120 [maxPwr]
00002009 [cornerPwr, maxPwr, 0, 0, f555, fCutSpeed, 1]
00002009 [0, 10, 0, 10, 0.0, f10000, 1]
00000120 [maxPwr, 0]
00000105 [256]            laser enable
00000608 [2000]
00000220 [1]              laser on
00000201 [f33333]
00000201 [fCutSpeed]
00020500 [0x304, dx, dy, L]   ...one per segment, L = round(hypot(dx, dy))
00000220 [0]              laser off
00000105 [0] x2
00000608 [2000]
00400C0B [id]
```

The 7-argument `00002009` and the 2-argument `00000120` also carry a second laser tube's corner
and work power (args 3–4, arg 2; LaserCut's `LaserCutConnerPower2` / `LaserCutWorkPower2`, seen in
the two-tube parogers samples). This machine has one tube, so they are 0.

Repeating a layer ("cut 5 times" in LaserCut, seen in one job not in the repo) is written out in
the one layer section: after each pass the whole setup block from `00000420 [3000]` to
`00000608 [2000]` is sent again, then a rapid back over the overcut (`00030500 [0x304, 0, −12, 12, 7]`),
laser on and the same moves. Our writer has no repeat option.

Curves are just short line segments (~0.1 mm). LaserCut adds a 0.1 mm (11-step) overcut at the
end of closed shapes: after returning to the start point it continues 11 steps along the first
segment's direction. On curves (seen in the original `LASER.MOL`) the overcut follows the
first few points of the path instead, ending 0.1 mm along it; `molreader.overcut_start()` handles both.

**Planned-motion cut layers** (seen in an old job, `00002009` last arg 0 instead of 1): after the
two `00002009` records the whole layer is a run of `8000080B` blocks, the same nested encoding as
the frame sections (see there), split at ~512 words. The nested stream holds `01000105 [256]`,
`01000608 [2000]`, then per stretch `05000101` profile + `0203000C [L, h]` + `03020500 [dx, dy, L]`
moves, with `01000220 [1/0]` switching the laser for cuts and travel; it ends with
`01000220 [0]`, `01000105 [0]` ×2, `01000608 [2000]`. `molreader.flat_records()` turns it into the
flat records below. Its closed shapes overrun the start by about 0.2 mm.

Moving between separate shapes inside one layer:
```
00000220 [0]                                     laser off
00000101 [f555, f555, fVpeak, f55555, f22222222] rapid profile
00030500 [0x304, dx, dy, L, h]                   rapid move
00000220 [1]                                     laser on
00000201 [f33333]
00000201 [fCutSpeed]
```

### Frame sections

Both start with `00300C0B [id]` and `80600101 [603, …same 7 SUB603 params…]` (no trailing 0.0s)
and end with `00400C0B [id]`. Moves are grouped in `8000080B` blocks whose payload is a list
of nested records (same encoding, top byte = arg count):

| Nested | Meaning |
|---|---|
| `05000101 [v_in, v_out, v_peak, accel, jerk]` | speed profile for the following move(s) |
| `0203000C [L, h]` | total length of the profiled moves, decel distance |
| `03020500 [dx, dy, L]` | relative XY move |
| `01000220 [1 / 0]` | laser on / off |

- **Section 1 (SUB609)**: one block tracing the job outline down, right, up, left from the
  top-left, laser off. With another start corner LaserCut keeps that order but starts at the head's
  corner (top-right: left, down, right, up; confirmed by the M_origins top-right sample). A head
  1 step off its corner (the rounding above) gets a 1-step move first. With the head off the
  corners (centre, centre top) it first moves to the **top-left** corner, then traces down, right,
  up, left and **stays at the top-left corner**: no move back to the head.
  Corner speeds: LaserCut keeps moving through each corner at `v_in = v_out =` the speed at which
  neither motor's speed jumps by more than X/Y-MaxChangeSpeed (35 mm/s = 3888 steps/s, see
  "LaserCut machine settings"): 3888 at a right angle (F_two_layers, the root `LASER.MOL`), and
  `3888 / (1 + 1110/1571) = 2278.27` at the centre's diagonal-to-down corner (exact). On the 20 mm
  squares the right-angle corners are lower, 3495.5 (3496.74 for 2224-step edges): a second limit
  that grows with the edge length, rule not found. We stop at each corner
  (`v_in = v_out = 555`), which is slower but safe. It only changes the Frame preview's speed.
- **Section 2 (SUB610)**: `00002009 [4000, 4000, f555, f2777, 0]` and
  `00002009 [4000, 4000, 4000, 4000, f555, f2777, 0]` (40 % power at 25 mm/s), then a block moving
  2 mm (222/223 steps) out to the frame corner beyond the head's corner (top-left: −223, +222;
  top-right: +222, +222; head off the corners: the top-left one, e.g. centre −1334, +1333) and
  switching the laser on, then a block tracing the 2 mm-larger box in the same corner order with a
  single profile at `v_peak = 0.8 × 2777` and accel 38888, laser off at the end. It **never comes
  back to the head**, whatever the start: it ends at the frame corner, 2 mm from a corner start.
  The controller brings the head back to the start by itself: on the machine (2026-10-21), Frame on
  the panel with LaserCut's own `M_origins/centre` job traced the square and ended back at the
  centre. Our writer does the same (no return moves in either Frame section).

### Rapid-move speed profile

For a rapid move of length `L` steps with `v0 = 555`, `a = 55555`, `J = 22222222`, the full
there-and-back ramp distance is `dist(v) = (v² − v0²)/a + (v + v0)·a/J` (jerk-limited S-curve).
- Peak `v = v0 + k · (8 + 10/4096)` steps/s for an integer `k`. The grid step is exact: all 23
  sample peaks encode bit-for-bit with it.
- `k` is the largest value with `dist(v) ≈ L`. LaserCut's exact rounding rule is unknown (its
  `dist` lands within ±3 steps of `L`); we use the largest `k` with `dist(v) ≤ L`, which matches
  most samples and is otherwise one grid step (0.08 %) slower, never faster.
- `h = floor(dist(v) / 2)`: the decel distance. Exact in every sample.
- `L = round(hypot(dx, dy))`, also for `00020500` cut segments.

## Engraving (samples G, I, J, K, L)

Samples: G = 70 mm-wide test (400 mm/s), I = 20×20 mm square at 100 mm/s / 20 %,
J = 20 mm filled circle, K = two 5×20 mm bars with a 10 mm gap, L = the square bidirectional.

Layer section header:
```
00300C0B [id]
00000420 [3000]
00000220 [2]                 engrave mode
00000620 [overscan]          2777 = 25 mm
00000520 [0]
00014301 [4, fScanSpeed]     X axis scan speed
00010001 [3, f555]           Y axis step speed
00002009 [0, 10, 0.0, f10000, 1]
00000120 [power]
00002009 [0, 10, 0, 10, 0.0, f10000, 1]
00000120 [power, power2]      power2 = LaserEngravePower2, 6000 (LaserCut's default 60 %)
00000105 [256]               laser enable
```
`power2` is the second laser tube's engrave power (parogers samples: `LaserEngravePower2=60` in the
`.INI` gives 6000); this machine has one tube, and we copy 6000.
(The rapid to the first line's start is in the main section, same as for cuts.)

Per scan pass:
```
80000720 [hdr, data...]                       laser pattern for this pass
00000001 [4, f4444, fScanSpeed, f666666, f22222222]
00014000 [4, ±scanLen]                        X move (+ = left→right)
00040500 [3, dy]                              Y step to next line (after a return pass)
```
End: `00000105 [0]` (just before the last pass's data), then the terminator
`80000720 [0x00AE0002, 0x00250000, 0]` and `00400C0B [id]`.

### Laser pattern record (`80000720`)

- `hdr = (N << 16) | wordCount`. `N` = number of 32-step blocks in the burn region.
- Each data word = `(off << 16) | on`: burn `on` steps, then `off` steps without laser,
  in travel order (for a right-to-left pass, counted from the right).
- Runs cover exactly `32·N` steps (pad the last `off`). A pass that starts with a gap uses `on = 0`.
- The burn region starts `overscan` steps after the pass starts. The pass length is
  `scanLen = 2·overscan + R`, with `N = R // 32 + 1` (exact for all 1000 sample lines;
  "round up" fails when R is a multiple of 32).
- Pass extents (unidirectional): forward pass from the current X to `burnEnd + overscan + 1`
  (LaserCut: +0..2 from float rounding). The return pass goes back to where the forward pass
  started, or further left if the next line starts further left (`min(start, nextBurnStart − overscan)`).
  So on a circle, passes only grow leftward on the lower half and keep the widest start on the upper half.
- Lines run bottom to top (machine +Y), one `00040500 [3, dy]` between lines. Y spacing follows the
  absolute canvas position (11/12-step pattern), so it can't be matched exactly from outside LaserCut.
- Header word 12 (time estimate) for engraving ≈ Σ|pass| / scanSpeed + passes · scanSpeed / 666666.
- Header word 4 counts every `00020500`, `00014000`, `00040500` and `00030500` record in the
  layer sections, + the Frame section 1 moves (see "Header").
- Unidirectional: return passes carry a fixed dummy `[(N << 16) | 2, 0x00250000, 0]`.
- Bidirectional (L): return passes carry real data and are shifted 22 steps (0.2 mm): LaserCut's
  "Acc space" setting, which depends on the speed (see "LaserCut machine settings"; L ran at
  100 mm/s). Every return pass leads with 22 off steps after the overscan where a forward pass
  has none, so the negative Acc space moves return-pass data left (−x) by |Acc space| × 111.111
  steps, rounded. We follow the table (`molwriter.acc_space`): 0.20 mm = 22 steps below
  200 mm/s, 0.25 mm = 28 below 400, 0.40 mm = 44 below 600, 0.35 mm = 39 from 600 (LaserCut's
  "up to 199" etc. read as inclusive). Only the 100 mm/s value is confirmed by a sample;
  `shift=` overrides it.
- Speed and power don't affect the pattern data (G and I are identical apart from those settings).
- Line pitch 0.1 mm here: `dy` alternates 11 / 12 steps.
- J (circle): each line's pass starts and ends just outside that line's own burn, so short
  lines get short passes.

## What our writer does differently from LaserCut (on purpose)

These are options in `EngraveLayer` / `svg2mol`; the defaults favour speed and were tested on the
machine.

- **Overscan** is worked out from the scan speed by default (`molwriter.auto_overscan`: the
  engrave ramp distance × 1.2 + 1 mm, about 4.4 mm at 100 mm/s, 25 mm at 400 mm/s,
  35.6 mm at 500 mm/s). LaserCut always used 25 mm (`overscan=25` restores it).
- **Laser-off return passes** (one-direction engraving) get their own `00000001` profile at
  300 mm/s, or the scan speed if that's faster. LaserCut ran them at scan speed (`return=0` restores it).
  The machine accepts a different speed per pass.
- **Engrave line positions** are computed from the design (centre of each line-spacing band,
  measured up from the bottom), so the 11/12-step spacing pattern can differ from LaserCut's by one step.

## Known approximations

`tools/test_roundtrip.py` reports these as `approx` instead of failing:

- Frame section 1: we stop at the corners (see "Frame sections"), and at the end of the
  diagonal from a centre head (LaserCut ends it at 2278 steps/s, see there).
- Frame section 2: the 2 mm margin can be one step different (LaserCut rounds in canvas coordinates).
- Positions LaserCut rounds in canvas coordinates can be 1 step off: the head against the box
  (words 6–9, M_origins), the overcut (12 instead of 11 steps in the top-right sample).
- Rapid peak speed: sometimes one 8 steps/s grid step lower (see "Rapid-move speed profile").
- Engrave pass ends: 1–2 steps (0.01–0.02 mm) shorter, inside the overscan where the laser is off.
- Header word 12 (time estimate): within a few seconds.

## Open questions

- `00000420 [3000]`, `00000608 [2000]` and `00002009 [0, 10, …, f10000, 1]` are copied unchanged;
  their meaning is unknown, and no LaserCut machine setting has these values.
- Header word 3 (always 1) and the low byte of word 5 (`0x1`): unknown, copied unchanged.
- The lower Frame section 1 corner speed on small jobs (31.5 mm/s on the 20 mm squares).
- Photo / greyscale engraving: no sample yet. It probably uses the same `80000720` records with
  per-run power, or a different record. A LaserCut sample of a small greyscale image would show it.
- The `.TXT` dump has `CMD106` values (per-segment numbers) that don't appear in the binary.

## LaserCut machine settings

LaserCut's Machine Options on the old PC (photos, 2026-10-21) and where they show up in the `.MOL`:

| Setting | Value | In the `.MOL` |
|---|---|---|
| Worktable pulse unit X, Y | 0.009 mm | 111.111 steps/mm (`00015001`) |
| Feeding pulse unit | 0.000625 mm | 1600 steps/mm, axis 2 |
| Start speed | 5 mm/s | 555, start and end of every profile |
| Quick speed | 300 mm/s | 33333, rapid speed cap |
| work-Acc | 350 mm/s² | 38888, laser frame (and cutting) |
| space-Acc (Cut: Corner acc) | 500 mm/s² | 55555, rapids and Frame section 1 |
| X/Y-MaxChangeSpeed | 35 mm/s | 3888 (TXT `CMD105`), Frame section 1 corner limit |
| Range | 1250 × 900 mm | bed size, not in the file |
| Cut: Curve disperse | 0.1 mm | curve segment length |
| Engrave: Acc len | 25 mm | LaserCut's overscan (2777) |
| Engrave: Start speed | 40 mm/s | 4444, end speed of scan passes |
| Engrave: Acc space | −0.20 mm up to 199 mm/s, −0.25 to 399, −0.40 to 599, −0.35 above | bidirectional return-pass shift (22 steps in L; `acc_space`) |
| Engrave dir | Down To (up) | lines run bottom to top |
| PWM frequency | 20000 Hz | TXT `SET002` only, not in the `.MOL` |

The jerk (22222222) is not in these pages; Const speed (30 mm/s), the test speeds and the Feeding
axis speeds are not in the file. LaserCut also slows small circles (Cut: Circle speed table, e.g. 10 mm/s below 0.55 mm radius); our
writer doesn't.

## Other LaserCut versions

Files from LaserCut DLL 4.1.x (header word 2 `0x04010300`, e.g. the parogers samples) have the same
record structure and arguments but different low opcode bytes (e.g. section start `00300048`
instead of `00300C0B`, power record `00000E46` instead of `00002009`), and some setups have no jerk
term in the speed profiles. Our tools read only `0x04030000` files.

## LaserCut project files (.ECP)

Decoded from one project (`Laser_File_Card_Back.ecp`, LaserCut 5.3: an engraved business card with
a cut outline), checked against the `.MOL` made from it. `tools/ecpreader.py` reads them;
`tools/cfb.py` lists or extracts the streams of any compound file.

- The file is a Microsoft compound document (OLE2, like old `.doc` files) holding one stream, `EC`.
- `EC` is a flat run of tagged values: a uint16 type, then the value. Types are Windows VARIANT
  codes: 2 int16, 3 int32, 4 float32, 5 float64, 8 string (uint32 byte count + UTF-16; LaserCut
  stores ANSI/GBK text widened byte by byte, e.g. its default layer name 新层 "new layer"),
  0x0B bool (int16), 0x11 byte. The whole stream tokenizes with nothing left over.
- Start: int32 1000 (version), four doubles (design box), the original path
  (`C:\LaserCut53\\laser.ECP`), then a **layer table** (count, then per layer: name, int16
  layer number 10, 20, 30, …, and 11 values later the colour as a Windows COLORREF `0x00BBGGRR`).
  Layer 10 is black, 20 blue, 30 red, 40 green, …, 70 cyan.
- Then the object count and one object per drawing element, each starting with the string
  `Ec.EcDbGraphics.1` and a type:
  - `1` line: x0, y0, x1, y1 (doubles)
  - `5` polyline: segment count, then per segment `[int32 kind, x0, y0, x1, y1, byte]`;
    kind 1 = straight (the only kind seen: LaserCut had already turned text into outlines, so arcs
    and Béziers are not decoded yet and the reader stops with a clear error)
  - `7` point list: count, then x, y pairs (circles, as 36 segments)
  - `0` the document's root object (no geometry)

  After the geometry comes a byte and 42 values of per-object properties; the object's layer number
  is the 40th of them. Coordinates are mm on LaserCut's canvas, **+y up**.
- **Settings are not in the .ECP.** LaserCut writes them to `NAME.INI` next to it (case may
  differ): `[Technics_Laser_Data]` has `LogicOrgPositionX/Y` (the job origin, i.e. where the head
  starts; the centre of the design in this project) and `[TechnicsLayers_<COLORREF>]` one section
  per layer colour: `TechnicsMode` (`TECHNICS_LASERCUT` / `TECHNICS_LASERENGRAVE`), `LaserCutSpeed`,
  `LaserCutWorkPower`, `LaserCutConnerPower`, `LaserEngraveSpeed`, `LaserEngravePower`,
  `LaserEngravePower2`, `LaserEngraveScanStepLen` (line spacing), `LaserEngraveTwoDirector`
  (both directions), and many more that we don't use.
- The cut shapes match the `.MOL`'s within 0.005 mm; LaserCut re-splits curves into ~0.1 mm
  segments when it writes the `.MOL`.

## Samples

`laser decode/<NAME>/LASER.MOL` + `LASER.TXT`, made in LaserCut on the old PC by changing one thing
at a time from A:

| Sample | What changed |
|---|---|
| A_square | 20×20 mm square, cut, 10 mm/s, 50 % |
| B_speed | speed 20 mm/s |
| C_Power | power 30 % (speed was left at 20) |
| D_corner | meant to be corner power 55 %; identical to C (setting didn't save) |
| H_new_corner | power 30 %, corner power 20 %, 10 mm/s, different position |
| E_circle | 20 mm circle |
| F_two_layers | two squares at 50 % and 30 % |
| G_engrave | 70 mm-wide engrave test at 400 mm/s |
| I_new_engrave | 20×20 mm filled square, engrave 100 mm/s, 20 % |
| J_engrave_circle | 20 mm filled circle, engrave |
| K_engrave_bars | two 5×20 mm bars, 10 mm gap |
| L_bidir | I in both directions |
| M_origins/… | 20×20 mm square, cut 7 mm/s, 85 %; start top-left (`tl`), top-right (`tr`), centre, centre top, top-left non-relative |

To decode something new, make two LaserCut files that differ in one setting and compare them:
`cmp -l a.MOL b.MOL` for bytes, or `tools/mol_inspect.py FILE dump` on both and `diff`.

