#!/usr/bin/env python3
"""Check file_dialogs.py without a Linux desktop: the portal, zenity and kdialog are faked.
macOS (not Linux) must call tkinter.filedialog with the caller's arguments unchanged.
The portal tests use the real jeepney (to build and parse the D-Bus messages) and are skipped
when it isn't installed (it is only installed on Linux)."""
import os
import sys
import tempfile
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import file_dialogs  # noqa: E402

try:
    import jeepney
    from jeepney.low_level import Parser
except ImportError:
    jeepney = None

OPEN = dict(filetypes=[('Drawings', '*.svg *.SVG *.dxf *.DXF'), ('SVG', '*.svg'), ('DXF', '*.dxf')],
            initialdir='/home/u/designs')
SAVE = dict(initialfile='JOB.MOL', defaultextension='.MOL', initialdir='/home/u/jobs',
            filetypes=[('Leetro job', '*.MOL')])


class FakeTk:
    def __init__(self):
        self.calls = []

    def call(self, *args):
        self.calls.append(args)
        return 'x11' if args == ('tk', 'windowingsystem') else ''


class FakeRoot:
    def __init__(self):
        self.tk = FakeTk()
        self.updates = 0

    def update(self):
        self.updates += 1

    def wm_frame(self):
        return '0x2a00007'

    def __str__(self):
        return '.'


class FakeProc:
    def __init__(self, code, out=b''):
        self.returncode, self.stdout = code, mock.Mock()
        self.stdout.read.return_value = out

    def poll(self):
        return self.returncode


class Base(unittest.TestCase):
    def setUp(self):
        self.root = FakeRoot()
        patches = [mock.patch.object(sys, 'platform', 'linux'),
                   mock.patch.dict(os.environ, {'LASER_DIALOGS': ''}),
                   mock.patch.object(file_dialogs.filedialog, 'askopenfilename', return_value='TK'),
                   mock.patch.object(file_dialogs.filedialog, 'asksaveasfilename', return_value='TK')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class NotLinux(unittest.TestCase):
    def test_macos_calls_tk_unchanged(self):
        with mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(file_dialogs.filedialog, 'askopenfilename', return_value='/a.svg') as ask, \
                mock.patch.object(file_dialogs.filedialog, 'asksaveasfilename', return_value='') as save, \
                mock.patch.object(file_dialogs, '_linux') as linux:
            root = object()
            self.assertEqual(file_dialogs.askopenfilename(parent=root, **OPEN), '/a.svg')
            ask.assert_called_once_with(parent=root, **OPEN)
            self.assertEqual(file_dialogs.asksaveasfilename(parent=root, **SAVE), '')
            save.assert_called_once_with(parent=root, **SAVE)
            linux.assert_not_called()


class Fallbacks(Base):
    def test_nothing_native_falls_back_to_tk(self):
        with mock.patch.object(file_dialogs, '_connect', side_effect=OSError('no bus')), \
                mock.patch('shutil.which', return_value=None):
            self.assertEqual(file_dialogs.askopenfilename(parent=self.root, **OPEN), 'TK')
            file_dialogs.filedialog.askopenfilename.assert_called_once_with(parent=self.root, **OPEN)
            self.assertEqual(file_dialogs.asksaveasfilename(parent=self.root, **SAVE), 'TK')
            file_dialogs.filedialog.asksaveasfilename.assert_called_once_with(parent=self.root, **SAVE)

    def test_forced_tk(self):
        with mock.patch.dict(os.environ, {'LASER_DIALOGS': 'tk'}), \
                mock.patch.object(file_dialogs, '_portal') as portal:
            self.assertEqual(file_dialogs.askopenfilename(parent=self.root, **OPEN), 'TK')
            portal.assert_not_called()

    def test_zenity_then_kdialog(self):
        """No portal; zenity can't open a display (exit 255), so kdialog is asked."""
        procs = [FakeProc(255), FakeProc(0, b'/home/u/designs/a b.svg\n')]
        with mock.patch.object(file_dialogs, '_portal', side_effect=file_dialogs.Unavailable), \
                mock.patch('shutil.which', side_effect=lambda n: '/usr/bin/' + n), \
                mock.patch('subprocess.Popen', side_effect=procs) as popen:
            self.assertEqual(file_dialogs.askopenfilename(parent=self.root, **OPEN), '/home/u/designs/a b.svg')
            self.assertEqual(popen.call_args_list[0].args[0][0], '/usr/bin/zenity')
            self.assertEqual(popen.call_args_list[1].args[0], [
                '/usr/bin/kdialog', '--title', 'Open', '--getopenfilename', '/home/u/designs',
                '*.svg *.SVG *.dxf *.DXF|Drawings\n*.svg|SVG\n*.dxf|DXF'])
        file_dialogs.filedialog.askopenfilename.assert_not_called()


class Zenity(Base):
    def run_zenity(self, proc, save=False, **extra):
        with mock.patch.dict(os.environ, {'LASER_DIALOGS': 'zenity'}), \
                mock.patch('shutil.which', return_value='/usr/bin/zenity'), \
                mock.patch('subprocess.Popen', return_value=proc) as popen:
            ask = file_dialogs.asksaveasfilename if save else file_dialogs.askopenfilename
            result = ask(parent=self.root, **(SAVE if save else OPEN), **extra)
        return result, popen.call_args

    def test_open(self):
        path, call = self.run_zenity(FakeProc(0, b'/home/u/designs/box.svg\n'))
        self.assertEqual(path, '/home/u/designs/box.svg')
        self.assertEqual(call.args[0], [
            '/usr/bin/zenity', '--file-selection', '--title=Open', '--filename=/home/u/designs/',
            '--file-filter=Drawings | *.svg *.SVG *.dxf *.DXF', '--file-filter=SVG | *.svg',
            '--file-filter=DXF | *.dxf'])
        # the window was kept drawn but held busy, and released afterwards
        self.assertIn(('tk', 'busy', 'hold', self.root), self.root.tk.calls)
        self.assertEqual(self.root.tk.calls[-1], ('tk', 'busy', 'forget', self.root))

    def test_cancel(self):
        path, _ = self.run_zenity(FakeProc(1))
        self.assertEqual(path, '')
        file_dialogs.filedialog.askopenfilename.assert_not_called()

    def test_save_adds_extension(self):
        path, call = self.run_zenity(FakeProc(0, b'/home/u/jobs/BOX\n'), save=True)
        self.assertEqual(path, '/home/u/jobs/BOX.MOL')
        self.assertEqual(call.args[0][:5], ['/usr/bin/zenity', '--file-selection', '--title=Save As',
                                            '--save', '--confirm-overwrite'])
        self.assertIn('--filename=/home/u/jobs/JOB.MOL', call.args[0])

    def test_save_added_extension_asks_before_replacing(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, 'BOX.MOL'), 'w').close()
            with mock.patch.object(file_dialogs.messagebox, 'askyesno', return_value=False) as ask:
                path, _ = self.run_zenity(FakeProc(0, os.path.join(d, 'BOX').encode() + b'\n'), save=True)
            self.assertEqual(path, '')
            ask.assert_called_once()

    def test_env_restored_in_bundle(self):
        env = {'LASER_DIALOGS': 'zenity', 'LD_LIBRARY_PATH': '/tmp/_MEI/lib', 'LD_LIBRARY_PATH_ORIG': '/opt/lib',
               'TCL_LIBRARY': '/tmp/_MEI/tcl', 'TK_LIBRARY': '/tmp/_MEI/tk', '_PYI_ARCHIVE_FILE': 'x',
               'HOME': '/home/u'}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, 'frozen', True, create=True):
            _, call = self.run_zenity(FakeProc(1))
        passed = call.kwargs['env']
        self.assertEqual(passed['LD_LIBRARY_PATH'], '/opt/lib')
        for var in ('LD_LIBRARY_PATH_ORIG', 'TCL_LIBRARY', 'TK_LIBRARY', '_PYI_ARCHIVE_FILE'):
            self.assertNotIn(var, passed)
        self.assertEqual(passed['HOME'], '/home/u')

    def test_env_without_original_library_path(self):
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/tmp/_MEI/lib'}), \
                mock.patch.object(sys, 'frozen', True, create=True):
            self.assertNotIn('LD_LIBRARY_PATH', file_dialogs.system_env())
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/mine'}):     # not bundled: left alone
            self.assertEqual(file_dialogs.system_env()['LD_LIBRARY_PATH'], '/mine')


def roundtrip(msg):
    """A message as the other side of the bus would see it (variants become (signature, value))."""
    p = Parser()
    p.add_data(msg.serialise(serial=7))
    return p.get_next_message()


class FakeBus:
    """The session bus with a portal that answers `response` (or fails with `fail`)."""

    def __init__(self, response=None, fail=None, handle=None):
        self.unique_name, self.sent, self.filters = ':1.42', [], []
        self.response, self.fail, self.handle, self.closed = response, fail, handle, False

    def filter(self, rule, bufsize=1):
        bus, queue = self, deque()

        class Handle:
            def __enter__(self):
                bus.filters.append((rule, queue))
                return queue

            def __exit__(self, *exc):
                bus.filters.remove((rule, queue))
        return Handle()

    def send_and_get_reply(self, msg, timeout=None):
        msg = roundtrip(msg)
        self.sent.append(msg)
        if msg.header.fields[jeepney.HeaderFields.member] in ('OpenFile', 'SaveFile'):
            if self.fail:
                return jeepney.new_error(msg, self.fail)
            token = msg.body[2]['handle_token'][1]
            handle = self.handle or f'/org/freedesktop/portal/desktop/request/1_42/{token}'
            signal = roundtrip(jeepney.new_signal(
                jeepney.DBusAddress(handle, interface='org.freedesktop.portal.Request'),
                'Response', 'ua{sv}', self.response))
            for rule, queue in self.filters:
                if rule.matches(signal):
                    queue.append(signal)
            return jeepney.new_method_return(msg, 'o', (handle,))
        return jeepney.new_method_return(msg)

    def recv_until_filtered(self, queue, timeout=None):
        return queue.popleft()

    def close(self):
        self.closed = True


@unittest.skipIf(jeepney is None, 'jeepney not installed')
class Portal(Base):
    def ask(self, bus, save=False):
        with mock.patch.object(file_dialogs, '_connect', return_value=bus), \
                mock.patch('shutil.which', return_value=None):
            ask = file_dialogs.asksaveasfilename if save else file_dialogs.askopenfilename
            return ask(parent=self.root, **(SAVE if save else OPEN))

    def call(self, bus):
        return [m for m in bus.sent if m.header.fields[jeepney.HeaderFields.member] in ('OpenFile', 'SaveFile')][0]

    def test_open(self):
        bus = FakeBus((0, {'uris': ('as', ['file:///home/u/designs/my%20box.svg'])}))
        with mock.patch('os.path.isdir', return_value=True):
            self.assertEqual(self.ask(bus), '/home/u/designs/my box.svg')
        call = self.call(bus)
        parent, title, opts = call.body
        self.assertEqual((parent, title), ('x11:2a00007', 'Open'))
        self.assertEqual(opts['filters'][1][0], ('Drawings', [(0, '*.svg'), (0, '*.SVG'), (0, '*.dxf'), (0, '*.DXF')]))
        self.assertEqual(opts['current_filter'][1], opts['filters'][1][0])
        self.assertEqual(opts['current_folder'][1], b'/home/u/designs\0')
        self.assertTrue(opts['modal'][1])
        self.assertTrue(bus.closed)
        self.assertEqual(self.root.tk.calls[-1], ('tk', 'busy', 'forget', self.root))
        file_dialogs.filedialog.askopenfilename.assert_not_called()

    def test_save(self):
        bus = FakeBus((0, {'uris': ('as', ['file:///home/u/jobs/BOX'])}))
        with mock.patch('os.path.isdir', return_value=True):
            self.assertEqual(self.ask(bus, save=True), '/home/u/jobs/BOX.MOL')
        call = self.call(bus)
        self.assertEqual(call.header.fields[jeepney.HeaderFields.member], 'SaveFile')
        opts = call.body[2]
        self.assertEqual(opts['current_name'][1], 'JOB.MOL')
        self.assertEqual(opts['filters'][1], [('Leetro job', [(0, '*.MOL')])])

    def test_cancel(self):
        self.assertEqual(self.ask(FakeBus((1, {}))), '')
        file_dialogs.filedialog.askopenfilename.assert_not_called()

    def test_old_portal_own_handle(self):
        bus = FakeBus((0, {'uris': ('as', ['file:///x.svg'])}), handle='/org/freedesktop/portal/desktop/request/7')
        self.assertEqual(self.ask(bus), '/x.svg')

    def test_no_file_chooser_falls_back(self):
        bus = FakeBus(fail='org.freedesktop.DBus.Error.UnknownMethod')
        self.assertEqual(self.ask(bus), 'TK')
        file_dialogs.filedialog.askopenfilename.assert_called_once()

    def test_no_missing_folder(self):
        bus = FakeBus((1, {}))
        with mock.patch('os.path.isdir', return_value=False):
            self.ask(bus)
        self.assertNotIn('current_folder', self.call(bus).body[2])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
