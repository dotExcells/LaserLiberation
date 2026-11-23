#!/usr/bin/env python3
"""Inspect Leetro LaserCut .MOL job files (reverse-engineered, work in progress).

Layout findings so far:
  - File is little-endian 32-bit words. Header block 0x000-0x1FF; code blocks
    start at 0x200 and subsequent sections are aligned to 0x200.
  - Each record starts with an opcode word:
      * bit 31 clear: top byte = number of argument words that follow.
      * bit 31 set:   the next word is the argument count (variable length).
  - Floats use Leetro's format: top byte = signed exponent e, low 24 bits =
    sign bit + 23-bit fraction; value = +/-(1 + frac/2^23) * 2^e.
  - Opcode 0x00020500 is a relative vector move: [axis_mask=0x304, dx, dy, len]
    in motor steps (111.111 steps/mm on X/Y for the sample machine).

Usage: mol_inspect.py FILE [hist | dump [START END [LIMIT]]]
"""
import collections
import struct
import sys


def leetro_float(w):
    w &= 0xFFFFFFFF
    if w == 0:
        return 0.0
    e = (w >> 24) & 0xFF
    if e >= 0x80:
        e -= 256
    m = w & 0xFFFFFF
    sign = -1 if m & 0x800000 else 1
    return sign * (1 + (m & 0x7FFFFF) / 2**23) * 2.0**e


def records(data, start=0x200, end=None):
    end = len(data) if end is None else end
    i = start
    while i < end:
        w = struct.unpack_from('<I', data, i)[0]
        if w == 0:
            i += 4
            continue
        if w & 0x80000000:
            n = struct.unpack_from('<I', data, i + 4)[0]
            a0 = i + 8
        else:
            n = w >> 24
            a0 = i + 4
        args = list(struct.unpack_from(f'<{n}i', data, a0))
        yield i, w & 0x80FFFFFF, args
        i = a0 + 4 * n


def main():
    data = open(sys.argv[1], 'rb').read()
    mode = sys.argv[2] if len(sys.argv) > 2 else 'hist'
    if mode == 'hist':
        hist = collections.Counter(op for _, op, _ in records(data))
        for op, count in sorted(hist.items()):
            print(f'{op:08x} {count}')
    else:
        start = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x200
        end = int(sys.argv[4], 0) if len(sys.argv) > 4 else None
        limit = int(sys.argv[5]) if len(sys.argv) > 5 else None
        for k, (off, op, args) in enumerate(records(data, start, end)):
            if limit is not None and k >= limit:
                break
            floats = [round(leetro_float(a), 3) for a in args[:8]]
            print(f'{off:06x} op={op:08x} n={len(args)} {args[:8]} f={floats}')


if __name__ == '__main__':
    main()
