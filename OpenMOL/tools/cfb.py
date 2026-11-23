#!/usr/bin/env python3
"""Minimal reader for Microsoft Compound File Binary files (OLE2, the container LaserCut's .ECP
project files use). Read-only, no dependencies.

Usage: cfb.py FILE            list the streams
       cfb.py FILE STREAM OUT  save one stream
"""
import struct
import sys

MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
FREE, END = 0xFFFFFFFF, 0xFFFFFFFE


class NotCfb(ValueError):
    pass


class CompoundFile:
    def __init__(self, data):
        if data[:8] != MAGIC:
            raise NotCfb('not a compound document')
        self.data = data
        self.sector = 1 << struct.unpack_from('<H', data, 0x1E)[0]
        self.mini_sector = 1 << struct.unpack_from('<H', data, 0x20)[0]
        (n_fat, dir_start, _, self.mini_cutoff, minifat_start, _, difat_start, n_difat) = \
            struct.unpack_from('<8I', data, 0x2C)
        difat = list(struct.unpack_from('<109I', data, 0x4C))
        s = difat_start
        for _ in range(n_difat):
            words = struct.unpack_from(f'<{self.sector // 4}I', self._sector(s))
            difat += words[:-1]
            s = words[-1]
        self.fat = []
        for s in difat[:n_fat]:
            self.fat += struct.unpack_from(f'<{self.sector // 4}I', self._sector(s))
        dirs = self._chain(dir_start)
        self.entries = []
        for i in range(0, len(dirs), 128):
            e = dirs[i:i + 128]
            n = struct.unpack_from('<H', e, 0x40)[0]
            name = e[:max(n - 2, 0)].decode('utf-16-le', 'replace')
            kind = e[0x42]
            left, right, child = struct.unpack_from('<3I', e, 0x44)
            start, size = struct.unpack_from('<IQ', e, 0x74)
            self.entries.append({'name': name, 'kind': kind, 'left': left, 'right': right,
                                 'child': child, 'start': start, 'size': size & 0xFFFFFFFF})
        root = self.entries[0]
        self.ministream = self._chain(root['start'])[:root['size']] if root['start'] < END else b''
        self.minifat = []
        if minifat_start < END:
            raw = self._chain(minifat_start)
            self.minifat = list(struct.unpack_from(f'<{len(raw) // 4}I', raw))

    def _sector(self, s):
        off = (s + 1) * self.sector
        return self.data[off:off + self.sector]

    def _chain(self, start, fat=None, read=None, size=None):
        fat = self.fat if fat is None else fat
        read = read or self._sector
        out, s, seen = [], start, set()
        while s < len(fat) and s not in seen and s not in (FREE, END):
            seen.add(s)
            out.append(read(s))
            s = fat[s]
        return b''.join(out)

    def _mini(self, s):
        off = s * self.mini_sector
        return self.ministream[off:off + self.mini_sector]

    def paths(self):
        """{'storage/stream': entry} for every stream, walking the directory tree."""
        out = {}

        def walk(i, prefix):
            if i >= len(self.entries) or i in (FREE, END):
                return
            e = self.entries[i]
            walk(e['left'], prefix)
            walk(e['right'], prefix)
            if e['kind'] == 2:
                out[prefix + e['name']] = e
            elif e['kind'] == 1:
                walk(e['child'], prefix + e['name'] + '/')
        walk(self.entries[0]['child'], '')
        return out

    def read(self, path):
        e = self.paths()[path]
        if e['size'] < self.mini_cutoff:
            return self._chain(e['start'], self.minifat, self._mini)[:e['size']]
        return self._chain(e['start'])[:e['size']]


def main():
    cf = CompoundFile(open(sys.argv[1], 'rb').read())
    if len(sys.argv) > 3:
        open(sys.argv[3], 'wb').write(cf.read(sys.argv[2]))
        return
    for path, e in sorted(cf.paths().items()):
        print(f'{e["size"]:10d}  {path}')


if __name__ == '__main__':
    main()
