# Leetro USB protocol: how LaserCut sends a `.MOL` to the machine

Decoded from USB recordings made on the old PC, in `USB capture/`:

- `capture1_square` (2026-10-19): 20 mm square, cut at 10 mm/s / 50 % (2560 bytes), with the
  `LASER.MOL` / `LASER.TXT` LaserCut saved for it
- `capture3_engrave` (2026-10-19): 20 mm filled square, engraved both directions at 100 mm/s / 20 %
  (15872 bytes), with its `LASER.MOL` / `LASER.TXT`
- `capture4_delete` (2026-10-20): LaserCut deleting the machine's three files (no job sent)

In the downloads, the bytes sent are the saved `.MOL`, byte for byte.

## Device

- USB 2.0, VID `0x0548`, PID `0x1005`, one configuration, one interface of class `0xFF`
  (vendor specific: no Windows class driver, so libusb can talk to it directly).
- Four bulk endpoints, 512-byte packets: `0x04` OUT and `0x88` IN carry everything below;
  `0x02` OUT and `0x86` IN were only reset when LaserCut opened the device, never used.
- LaserCut opens it with SET_CONFIGURATION 1 and resets all four pipes.

## Link layer: one exchange per command

Every command is the same handshake (bytes as sent on the bulk endpoints):

| Dir | Bytes | Meaning |
|---|---|---|
| OUT | `01 05` | host: request to send (ENQ) |
| IN  | `04` | device: ready |
| OUT | command frame | |
| IN  | `06` | device: received (ACK) |
| IN  | `05` | device: reply waiting (ENQ) |
| OUT | `01 04` | host: ready |
| IN  | reply frame | |
| OUT | `01 06` | host: received (ACK) |

**Command frame** (host → device): `[n] [dataLen] 01 [cmd] [data…] [chk]`. `n` is the number of
bytes after it, `dataLen` the length of `data`, `01` probably the device address. `chk` makes the
`n` bytes after the first sum to 0 (mod 256).

**Reply frame** (device → host): `[dataLen] 01 [cmd] [data…] [status] [chk]`. `status` was always
`00`. `chk` makes the whole frame sum to 0 (mod 256).

## Commands seen

| cmd | Data sent | Reply data | What it seems to do |
|---|---|---|---|
| `89` | none | 2 bytes `[a] [files]` | status poll (LaserCut polls about every 1.3 s). Second byte: number of files on the machine. First byte: the number of the newest file; it keeps its value after a delete (`03 03` → `03 00`) and is 1 again after the next send |
| `10` | 1 byte: block number (`17`, `18`, `1A`) | `[len] [len bytes]` | read a parameter block (`17`: 4 × `14 45 01 95`, `18`: 16 × `FF`, `1A`: 4 × `FF`) |
| `10` | block number + bytes (`19` + 16 × `00`, `1B` + 4 × `FF`, `1D` + 4 × `00`) | none | write a parameter block |
| `88` | none | none | **delete all files** on the machine (status went from 3 files to 0) |
| `80` | file name (8.3, space padded, no dot: `LASER   MOL`) + file size (uint32 LE) | none | start a file download |
| (bulk) | the `.MOL` file, raw, in 512-byte packets on `0x04`, no framing, all in one go (31 packets for the engrave file, no replies in between) | | the file itself, byte for byte |
| `85` | uint32 LE: sum of all file bytes (`0x000091AF`, `0x0007BBD9`) | none | file checksum |
| `82` | none | none | end of download |
| `83` | 1 byte: file index (`01`) | file name + size (uint32) | read the machine's file list |

Connecting: `89` ×2, `10` read `17`, `18`, `1A`, `10` write `19`, `1B`, `1D`, then `88` if LaserCut
deletes the machine's files (first and delete recordings), `89`. Download: `89`, `80`, file data,
`85`, `82`, `89`, `83 01`.
The parameter blocks read and written were identical in both recordings.

**The machine keeps every file sent to it**, even with the same name: a send adds a new entry at the
end of its file list (confirmed from the Mac, 2026-10-20: two `LASER.MOL` entries afterwards). The
panel can't choose between them; it shows and runs the newest. LaserCut's usual workflow is to delete
all files (`88`) and then send.

## From the Mac

`tools/leetro_usb.py` speaks this protocol with pyusb + libusb (no driver needed on macOS). By
default it only reads: status (`89`), file list (`83`) and, with `--params`, the parameter reads
(`10`). `leetro_usb.py send FILE.MOL` sends a job like LaserCut: connect sequence with its parameter
writes, delete all files (`88`, then checks the machine lists none; `--keep` skips this), `89`, `80`,
file data, `85`, `82`, `89`, `83`. It checks the file is a whole `.MOL` and asks for confirmation
first. `tools/test_leetro_usb.py` replays every read exchange, both recorded downloads and the
recorded delete, and checks every byte we send is LaserCut's.

**Run against the machine from the Mac (2026-10-20):** connected without any driver; status and file
list answered correctly (1 file, `LASER.MOL`, 102912 bytes). Then `send machine_tests/1_square_low_power/LASER.MOL`:
every command was accepted and the machine listed the file as entry 2 (`LASER.MOL`, 2560 bytes),
next to the old one. Then `machine_tests/6_letter_B` (16896 bytes) was sent the same way and **cut
correctly** from the panel. The panel can't choose between files: it shows and runs the latest one.
Delete-then-send from the Mac also works: a send onto a machine with one file deleted it and left
only the new one (status `01 01`, file 1 of 1).

## Open questions

- What the parameter blocks `17`–`1D` are, and whether the writes are needed (same values both
  times, so copying them is safe).
- Whether the machine limits the file size (it held a 102.9 kB file; the largest sent over USB so
  far is 15.9 kB; our engrave files can be bigger still).
- How to start a job from the computer, if LaserCut can; so far jobs are started on the panel.
