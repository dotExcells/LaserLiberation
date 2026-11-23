#!/usr/bin/env python3
"""Talk to the Leetro laser over USB, the way LaserCut does (see USB_PROTOCOL.md).

Usage: leetro_usb.py                        status and file list (read-only)
       leetro_usb.py --params               also read the parameter blocks LaserCut reads (17, 18, 1A)
       leetro_usb.py send FILE.MOL          delete all files on the machine, then send the job (asks first)
       leetro_usb.py send --keep FILE.MOL   send without deleting (the machine adds it as a new file)
       leetro_usb.py -v ...                 also print every byte exchanged

Sending repeats what LaserCut did in the recordings: the same connect sequence (including its
parameter writes, same values every time), then "delete all files" like LaserCut's usual workflow,
then the file, its checksum and the end command, and finally checks the machine's file list. The
panel shows and runs the latest file; start it there. Don't send while a job is running.

Needs pyusb and libusb-package (in requirements.txt). On macOS and Linux no driver is needed.
On Windows the machine uses LaserCut's own driver, which libusb can't use; this hasn't been tried there.
"""
import argparse
import errno
import os
import sys

VID, PID = 0x0548, 0x1005
EP_OUT, EP_IN = 0x04, 0x88
TIMEOUT_MS = 2000
DATA_TIMEOUT_MS = 10000
PACKET = 512
ENQ, EOT, ACK = 0x05, 0x04, 0x06

STATUS, READ_PARAMS, FILE_ENTRY = 0x89, 0x10, 0x83           # reads
START_FILE, CHECKSUM, END_FILE = 0x80, 0x85, 0x82            # used only by send_file()
DELETE_ALL = 0x88                                            # deletes every file on the machine
# LaserCut's connect sequence: parameter blocks it writes, with the values it always wrote
CONNECT_WRITES = [(0x19, bytes(16)), (0x1B, b'\xff' * 4), (0x1D, bytes(4))]
MACHINE_NAME = 'LASER   MOL'                                 # 8.3 name, space padded, no dot


class LeetroError(RuntimeError):
    pass


class AccessDenied(LeetroError):
    """Linux: the laser is there, but this user may not open it (no udev rule yet)."""


LIBUSB_ERROR_ACCESS = -3


def access_denied(e):
    """Is this pyusb USBError "permission denied" (EACCES / LIBUSB_ERROR_ACCESS)?"""
    return e.errno == errno.EACCES or getattr(e, 'backend_error_code', None) == LIBUSB_ERROR_ACCESS


NO_ACCESS = ("this computer isn't allowed to use the laser yet: click Send to laser in Laser Prepare "
             'to allow it, or run linux/install-udev.sh once; then unplug and replug the laser')


def frame(cmd, data=b''):
    """Command frame: [n] [dataLen] 01 [cmd] [data] [chk]; the n bytes after the first sum to 0."""
    body = bytes([len(data), 0x01, cmd]) + bytes(data)
    body += bytes([-sum(body) & 0xFF])
    return bytes([len(body)]) + body


def parse_reply(raw, cmd):
    """Reply frame: [dataLen] 01 [cmd] [data] [status] [chk]; the whole frame sums to 0."""
    if len(raw) < 5 or sum(raw) & 0xFF:
        raise LeetroError(f'bad reply to command {cmd:#04x}: {raw.hex(" ")}')
    n = raw[0]
    if raw[1] != 0x01 or raw[2] != cmd or len(raw) != n + 5:
        raise LeetroError(f'unexpected reply to command {cmd:#04x}: {raw.hex(" ")}')
    if raw[3 + n] != 0:
        raise LeetroError(f'command {cmd:#04x} failed: status {raw[3 + n]:#04x}')
    return raw[3:3 + n]


def is_read(cmd, data):
    return cmd in (STATUS, FILE_ENTRY) or (cmd == READ_PARAMS and len(data) == 1)


class Link:
    """The ENQ / ACK handshake around each command. `io` has write(bytes) and read() -> bytes."""

    def __init__(self, io, verbose=False):
        self.io, self.verbose, self.pending = io, verbose, b''

    def _write(self, data):
        if self.verbose:
            print(f'  > {data.hex(" ")}')
        self.io.write(data)

    def _read(self, n=None):
        """Read one expected unit: a single handshake byte (n=1) or a whole reply frame (n=None).
        The device may put several of them in one USB packet, so keep what's left over."""
        while not self.pending:
            self.pending = bytes(self.io.read())
            if self.verbose:
                print(f'  < {self.pending.hex(" ")}')
        if n is None:
            n = self.pending[0] + 5
            while len(self.pending) < n:
                more = bytes(self.io.read())
                if self.verbose:
                    print(f'  < {more.hex(" ")}')
                self.pending += more
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def _expect(self, byte, what):
        got = self._read(1)
        if got != bytes([byte]):
            raise LeetroError(f'expected {what} ({byte:#04x}) from the machine, got {got.hex(" ")}')

    def command(self, cmd, data=b'', write=False):
        """One command; anything that isn't a read needs write=True."""
        if not write and not is_read(cmd, data):
            raise LeetroError(f'command {cmd:#04x} is not a read')
        self._write(bytes([1, ENQ]))
        self._expect(EOT, 'ready')
        self._write(frame(cmd, data))
        self._expect(ACK, 'received')
        self._expect(ENQ, 'reply waiting')
        self._write(bytes([1, EOT]))
        reply = parse_reply(self._read(), cmd)
        self._write(bytes([1, ACK]))
        return reply

    def raw(self, data):
        """Send file data as-is in 512-byte packets (no handshake, like LaserCut)."""
        for i in range(0, len(data), PACKET):
            if self.verbose:
                print(f'  > [{min(PACKET, len(data) - i)} bytes of file data]')
            self.io.write(data[i:i + PACKET], bulk=True)


def status(link):
    """(state, number of files) from command 89. State seen: 0 with no files, 1 ready,
    2 straight after one of our downloads (meaning not known yet)."""
    r = link.command(STATUS)
    return r[0], r[1]


def file_entry(link, index):
    """(name, size) of the machine's file number `index` (from 1)."""
    r = link.command(FILE_ENTRY, bytes([index]))
    raw = r[:11].decode('ascii', 'replace')
    name = raw[:8].rstrip() + ('.' + raw[8:].rstrip() if raw[8:].strip() else '')
    return name, int.from_bytes(r[11:15], 'little')


def read_params(link, block):
    r = link.command(READ_PARAMS, bytes([block]))
    return r[1:1 + r[0]]


def check_job(data):
    """Refuse anything that isn't a whole .MOL job (header, size, 512-byte blocks)."""
    sys.path.insert(0, os.path.dirname(__file__))
    from molreader import NotAMolFile, check
    try:
        check(data)
    except NotAMolFile as e:
        raise LeetroError(f'not sending: {e}')
    if len(data) % PACKET:
        raise LeetroError(f'not sending: {len(data)} bytes is not a whole number of 512-byte blocks')


def connect(link):
    """LaserCut's connect sequence, as recorded."""
    status(link)
    status(link)
    for block in (0x17, 0x18, 0x1A):
        read_params(link, block)
    for block, value in CONNECT_WRITES:
        link.command(READ_PARAMS, bytes([block]) + value, write=True)


def delete_all(link):
    """Delete every file on the machine (LaserCut does this before sending) and check it worked."""
    link.command(DELETE_ALL, write=True)
    _, count = status(link)
    if count:
        raise LeetroError(f'asked the machine to delete all files, but it still lists {count}')


def send_file(link, data, do_connect=True, delete=False):
    """Send a .MOL as the machine's LASER.MOL, like LaserCut, deleting all files first if `delete`.
    Returns (file number, name, size) as the machine lists it afterwards."""
    check_job(data)
    if do_connect:
        connect(link)
    if delete:
        delete_all(link)
    status(link)
    link.command(START_FILE, MACHINE_NAME.encode('ascii') + len(data).to_bytes(4, 'little'), write=True)
    link.raw(data)
    link.command(CHECKSUM, (sum(data) & 0xFFFFFFFF).to_bytes(4, 'little'), write=True)
    link.command(END_FILE, write=True)
    _, count = status(link)
    if count < 1:
        raise LeetroError('sent, but the machine now lists no files')
    name, size = file_entry(link, count)        # the machine adds the new file at the end
    if name != 'LASER.MOL' or size != len(data):
        raise LeetroError(f'sent, but the last file on the machine (number {count}) is {name}, {size} bytes')
    return count, name, size


def usb_modules():
    """(usb, libusb_package), or raise LeetroError if they aren't installed."""
    try:
        import libusb_package
        import usb.core
        import usb.util
    except ImportError:
        raise LeetroError('USB support is missing: run with the project venv '
                          '(.venv/bin/pip install -r requirements.txt)')
    return usb, libusb_package


LIBUSB = {'darwin': 'libusb-1.0.dylib', 'win32': 'libusb-1.0.dll'}.get(sys.platform, 'libusb-1.0.so')


def backend(libusb_package):
    """libusb for pyusb: libusb-package's own copy, or in the Laser Prepare app (PyInstaller) the
    copy bundled next to the code, which the package can't find there by itself."""
    found = libusb_package.get_libusb1_backend()
    if found is None and getattr(sys, 'frozen', False):
        import usb.backend.libusb1
        for folder in (getattr(sys, '_MEIPASS', ''), os.path.dirname(sys.executable)):
            path = os.path.join(folder, LIBUSB)
            if os.path.exists(path):
                found = usb.backend.libusb1.get_backend(find_library=lambda _name, p=path: p)
                if found is not None:
                    break
    return found


def usb_check():
    """One line for --check-usb: does USB support load, and is the laser there?"""
    try:
        usb, libusb_package = usb_modules()
        be = backend(libusb_package)
        if be is None:
            return 'USB support: libusb not found'
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=be)
    except Exception as e:
        return f'USB support: not working ({e})'
    if dev is not None and sys.platform.startswith('linux'):
        node = f'/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}'    # only its permissions are checked
        if os.path.exists(node) and not os.access(node, os.R_OK | os.W_OK):
            return f'USB support: OK; laser connected, but {NO_ACCESS}'
    return f'USB support: OK; laser {"connected" if dev is not None else "not connected"}'


class UsbIO:
    def __init__(self):
        usb, libusb_package = usb_modules()
        self.usb = usb
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend(libusb_package))
        if dev is None:
            raise LeetroError('laser not found: is it switched on and its USB cable plugged into this computer?')
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            dev.set_configuration(1)
            usb.util.claim_interface(dev, 0)
            for ep in (EP_OUT, EP_IN):
                dev.clear_halt(ep)
        except usb.core.USBError as e:
            if access_denied(e) and sys.platform.startswith('linux'):
                raise AccessDenied(NO_ACCESS)
            raise LeetroError(f'could not open the laser over USB: {e}')
        self.dev = dev

    def write(self, buf, bulk=False):
        self.dev.write(EP_OUT, buf, DATA_TIMEOUT_MS if bulk else TIMEOUT_MS)

    def read(self):
        try:
            return self.dev.read(EP_IN, PACKET, TIMEOUT_MS)
        except self.usb.core.USBTimeoutError:
            raise LeetroError('the machine did not answer (timeout)')

    def close(self):
        self.usb.util.release_interface(self.dev, 0)
        self.usb.util.dispose_resources(self.dev)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', nargs='?', choices=['status', 'send'], default='status')
    ap.add_argument('file', nargs='?', help='send: the .MOL job')
    ap.add_argument('--params', action='store_true', help='also read parameter blocks 17, 18, 1A')
    ap.add_argument('-y', '--yes', action='store_true', help="send: don't ask for confirmation")
    ap.add_argument('--keep', action='store_true', help="send: don't delete the machine's files first")
    ap.add_argument('-v', '--verbose', action='store_true', help='print every byte exchanged')
    args = ap.parse_args(argv)
    if args.action == 'send':
        if not args.file:
            ap.error('send needs a .MOL file')
        with open(args.file, 'rb') as f:
            data = f.read()
        try:
            check_job(data)
        except LeetroError as e:
            sys.exit(f'{args.file}: {e}')
    io = None
    try:
        io = UsbIO()
        link = Link(io, args.verbose)
        state, count = status(link)
        print(f'connected to the laser (USB {VID:04x}:{PID:04x})')
        print(f'{count} file(s) on the machine (state byte: {state})')
        for i in range(1, count + 1):
            name, size = file_entry(link, i)
            print(f'  {i}. {name}  {size} bytes')
        if args.params:
            for block in (0x17, 0x18, 0x1A):
                print(f'parameter block {block:02X}: {read_params(link, block).hex(" ")}')
        if args.action == 'send':
            if args.keep or not count:
                what = f'send {args.file} ({len(data)} bytes) to the laser as LASER.MOL?'
            else:
                what = (f'delete all {count} file(s) on the machine, then send {args.file} '
                        f'({len(data)} bytes) as LASER.MOL?')
            print('\n' + what)
            if not args.yes:
                try:
                    answer = input('type y to send: ')
                except EOFError:
                    answer = ''
                if answer.strip().lower() != 'y':
                    print('not sent')
                    return
            number, name, size = send_file(link, data, delete=not args.keep)
            print(f'sent: the machine now has {name}, {size} bytes, as file {number} of {number}. '
                  'Start it on the panel.')
    except LeetroError as e:
        sys.exit(str(e))
    finally:
        if io:
            io.close()


if __name__ == '__main__':
    main()
