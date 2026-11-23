#!/bin/sh
# Link the laser extensions into Inkscape (macOS or Linux). Restart Inkscape afterwards.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(uname)" in
  Darwin) EXT="$HOME/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions" ;;
  Linux)  EXT="${XDG_CONFIG_HOME:-$HOME/.config}/inkscape/extensions" ;;
  *) echo "On Windows, see DEVELOPMENT.md (Inkscape extensions aren't set up there yet)." >&2; exit 1 ;;
esac
mkdir -p "$EXT"
for f in laser_prepare.inx laser_prepare.py leetro_mol_output.inx leetro_mol_output.py; do
  ln -sf "$HERE/$f" "$EXT/$f"
done
echo "Installed into $EXT. Restart Inkscape, then use Extensions > Laser > Prepare for laser..."
