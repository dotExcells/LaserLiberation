# LaserLiberation: SVG / DXF → Leetro laser

Open-source replacement for Leetro's LaserCut software.
Draw in Inkscape (or any CAD program that saves DXF), set speed and power per colour in
**Laser Prepare**, then send the job to the laser over its USB cable or save it as a `.MOL` file
for a USB stick. Runs on macOS and Linux.

Made for and tested on one Leetro-controlled CO₂ laser (controller USB ID `0548:1005`). Other
Leetro machines that read LaserCut's `.MOL` files may work; please open an issue with what you find.
Not affiliated with Leetro.

## Download

Get the latest build from the [Releases](../../releases/latest) page:

- **macOS** (Apple Silicon): `Laser-Prepare-macOS.zip`. Unzip, move **Laser Prepare** to
  Applications. The app isn't signed by Apple: the first time, right-click it → **Open** → **Open**
  (or System Settings → Privacy & Security → **Open Anyway**).
- **Linux** (x86_64): `Laser_Prepare-x86_64.AppImage`. Make it executable
  (`chmod +x Laser_Prepare-x86_64.AppImage`) and double-click it. The first time you click
  **Send to laser**, the app asks for your password once to allow this computer to use the laser;
  then unplug and replug the laser.

Or build it yourself: see [DEVELOPMENT.md](DEVELOPMENT.md).

## Using Laser Prepare

Open a drawing with **Open drawing…** (or drop it on the app). The preview shows the job; the red
dot is where to put the laser head before starting. Each colour gets a row with its job and
settings, then **Send to laser**, **Save to USB stick** (macOS) or **Save as file…**.

- **SVG**: fill colour = engrave, outline colour = cut. Convert text to paths first.
- **DXF**: every line, arc, circle, spline and polyline is a cut, grouped by colour; HATCH areas
  are engraved. Units come from the file (unitless = mm). Explode text to outlines first.
- **Job per colour**: switch an outline colour to *Engrave* to engrave inside its closed shapes, or
  a filled colour to *Cut* to cut along its edges. Untick a colour to leave it out.
- **Recolour shapes** when the drawing's colours aren't set up: click or drag-select shapes in the
  preview, then pick one of the colour boxes. **Save recoloured copy…** writes a new SVG/DXF.
- **Settings…**: the start position (a corner or the centre of the design) and engraving in both
  directions.
- Settings are remembered per colour, so the next drawing with the same colours starts with them.
- A `.MOL` job (one of these, or an old LaserCut one) opens read-only and can be sent or saved
  again as it is.

Engraving runs first, then cutting; shapes inside others (holes) are cut first.

**Start position:** the laser head's position when the job starts is the design's top-left corner
(the machine's immediate mode, as in LaserCut), or the point picked in **Settings…**.

**Engraving needs room on both sides:** the head runs past the design (about 36 mm at 500 mm/s),
so leave that much space left and right of it.

**USB sticks:** the controller only reads sticks with a Master Boot Record partition scheme (FAT16
works). Put `.MOL` files in the root of the stick.

## Inkscape

The **Prepare for laser** window also runs inside Inkscape (*Extensions → Laser → Prepare for
laser…*), plus **File → Save a Copy… → Leetro LaserCut (*.MOL)**. They run from a copy of this
repository; see [DEVELOPMENT.md](DEVELOPMENT.md) to set it up, then run `inkscape/install.sh` and
restart Inkscape.

## Command line

From a copy of this repository (see [DEVELOPMENT.md](DEVELOPMENT.md)):

```bash
.venv/bin/python tools/svg2mol.py design.svg -o LASER.MOL --engrave black:speed=500,power=30 --layer red:speed=10,power=90,corner=60
.venv/bin/python OpenMOL/tools/leetro_usb.py send LASER.MOL       # send over the USB cable
python3 OpenMOL/tools/mol_preview.py LASER.MOL                    # draw what the job will do
.venv/bin/python OpenMOL/tools/mol2dxf.py OLDJOB.MOL              # old job or LaserCut .ECP → DXF + settings
```

`speed` is mm/s; `power` and `corner` (LaserCut's corner power) are %. `svg2mol.py --help` lists
the rest (line spacing, both directions, start position, saved settings files).

## What works

| | |
|---|---|
| Cutting: layers, speed, power, corner power | Done; rebuilds LaserCut's sample files byte for byte |
| Engraving filled shapes, one or both directions | Done |
| Photo / bitmap engraving | Not yet |
| Send to laser over USB | macOS and Linux |
| `.MOL` / LaserCut `.ECP` → DXF | Cut layers; engraving not converted yet |
| Windows | Not yet |

The `.MOL` format and USB protocol were worked out from LaserCut's own files and USB recordings:
[OpenMOL/tools/MOL_FORMAT.md](OpenMOL/tools/MOL_FORMAT.md),
[OpenMOL/tools/USB_PROTOCOL.md](OpenMOL/tools/USB_PROTOCOL.md).

**Use at your own risk.** A laser cutter can start fires. Test new jobs at low power on scrap and
never leave the machine running unattended.

## Licence

GPL-3.0, see [LICENSE](LICENSE). The bundled Sun Valley ttk theme (`tools/sv_ttk/`) is MIT.
