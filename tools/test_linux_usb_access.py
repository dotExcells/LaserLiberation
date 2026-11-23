#!/usr/bin/env python3
"""Check the Linux "allow this computer to use the laser" path without Linux, root or USB:
access-denied detection (and that macOS is unchanged), --check-usb's hint, the exact pkexec
command and environment, the root script (run for real on a scratch target with a fake udevadm:
the installed rule must equal linux/70-leetro-laser.rules byte for byte, the old 99- file goes),
cancel / refused / failure / no pkexec, the terminal fallback, and the window's dialogs. Run with
the project venv."""
import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(__file__))
import openmol_path  # noqa: E402,F401  (puts OpenMOL/tools on the path)
import leetro_usb  # noqa: E402
import linux_usb_access as lua  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
RULE = os.path.join(ROOT, 'linux', '70-leetro-laser.rules')
failures = []


def check(ok, what):
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        failures.append(what)


class Patch:
    """Set attributes for a with-block, then put them back."""
    def __init__(self, *triples):
        self.triples, self.saved = triples, []

    def __enter__(self):
        for obj, name, value in self.triples:
            self.saved.append((obj, name, getattr(obj, name, Patch)))
            setattr(obj, name, value)

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            if value is Patch:
                delattr(obj, name)
            else:
                setattr(obj, name, value)


# ---------------------------------------------------------------- detection (fake pyusb)

class USBError(IOError):
    def __init__(self, strerror, error_code=None, errno=None):
        IOError.__init__(self, errno, strerror)
        self.backend_error_code = error_code


def fake_usb(error=None, present=True):
    class Dev:
        bus, address = 1, 7

        def is_kernel_driver_active(self, i):
            if error:
                raise error
            return False

        def set_configuration(self, n):
            if error:
                raise error

        def clear_halt(self, ep):
            pass
    core = types.SimpleNamespace(USBError=USBError, find=lambda **kw: Dev() if present else None)
    util = types.SimpleNamespace(claim_interface=lambda d, i: None)
    return types.SimpleNamespace(core=core, util=util)


def open_error(platform, error, present=True):
    with Patch((leetro_usb, 'usb_modules', lambda: (fake_usb(error, present), None)),
               (leetro_usb, 'backend', lambda pkg: object()), (sys, 'platform', platform)):
        try:
            leetro_usb.UsbIO()
        except leetro_usb.LeetroError as e:
            return e
    return None


denied = USBError('Access denied (insufficient permissions)', -3, errno.EACCES)
e = open_error('linux', denied)
check(isinstance(e, leetro_usb.AccessDenied), 'Linux: EACCES on open is AccessDenied')
check(isinstance(open_error('linux', USBError('Access denied', -3, None)), leetro_usb.AccessDenied),
      'Linux: LIBUSB_ERROR_ACCESS without errno is AccessDenied')
check('Send to laser' in str(e) and 'install-udev.sh' in str(e), 'the message names both ways to allow it')
busy = open_error('linux', USBError('Resource busy', -6, errno.EBUSY))
check(type(busy) is leetro_usb.LeetroError, 'Linux: other USB errors stay plain errors')
gone = open_error('linux', None, present=False)
check(type(gone) is leetro_usb.LeetroError and 'not found' in str(gone), 'laser missing is "not found", not denied')
mac = open_error('darwin', denied)
check(type(mac) is leetro_usb.LeetroError and 'could not open the laser' in str(mac),
      'macOS: an access error is the same plain error as before (no setup offered)')

# --check-usb
for platform, allowed, want in [('linux', False, "isn't allowed"), ('linux', True, 'laser connected'),
                                ('darwin', False, 'laser connected')]:
    with Patch((leetro_usb, 'usb_modules', lambda: (fake_usb(), None)), (leetro_usb, 'backend', lambda pkg: object()),
               (sys, 'platform', platform), (os.path, 'exists', lambda p: True),
               (os, 'access', lambda p, m: allowed)):
        line = leetro_usb.usb_check()
    check(want in line and (want != 'laser connected' or "isn't" not in line), f'--check-usb {platform}, '
          f'{"allowed" if allowed else "not allowed"}: {line}')
check('Send to laser' in leetro_usb.NO_ACCESS, '--check-usb mentions the in-app option')

# ---------------------------------------------------------------- the root command

rule = open(RULE, 'rb').read()
check(lua.rule_bytes() == rule, 'the module reads linux/70-leetro-laser.rules')
script = lua.root_script()
check(lua.TARGET == '/etc/udev/rules.d/70-leetro-laser.rules', 'target is /etc/udev/rules.d/70-leetro-laser.rules')
check(lua.TARGET < '/etc/udev/rules.d/73-seat-late.rules', "the rule sorts before systemd's 73-seat-late.rules (uaccess)")
check(lua.OLD_TARGET == '/etc/udev/rules.d/99-leetro-laser.rules', 'old target is the 99- file')
check('rm -f /etc/udev/rules.d/99-leetro-laser.rules &&' in script, 'root script removes the old 99- file')
check(b'SUBSYSTEM' not in script.encode() and '$' not in script and '`' not in script,
      'root script is fixed text: no rule bytes, no expansions')
check(script.startswith('PATH=/usr/sbin:/usr/bin:/sbin:/bin; umask 022; '), 'root script sets its own PATH and umask')
check(lua.pkexec_argv('/usr/bin/pkexec') == ['/usr/bin/pkexec', '/bin/sh', '-c', script], 'pkexec argv')
spec = open(os.path.join(ROOT, 'app', 'Laser Prepare.spec')).read()
check("'linux', '70-leetro-laser.rules'), 'linux')" in spec, 'the app bundles the same rules file')
inst = open(os.path.join(ROOT, 'linux', 'install-udev.sh')).read()
check('"$HERE/70-leetro-laser.rules" /etc/udev/rules.d/70-leetro-laser.rules' in inst,
      'install-udev.sh installs the same file')
check('sudo rm -f /etc/udev/rules.d/99-leetro-laser.rules' in inst, 'install-udev.sh removes the old 99- file')
check('99-leetro' not in rule.decode(), 'the rule file has no old name in it')
check(lua.RELOAD in inst and lua.TRIGGER in inst, 'install-udev.sh reloads and triggers the same way')

scratch = tempfile.mkdtemp()
try:
    bindir = os.path.join(scratch, 'bin')
    os.mkdir(bindir)

    def tool(name, body):
        path = os.path.join(bindir, name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, 0o755)
        return path

    log = os.path.join(scratch, 'udevadm.log')

    old = os.path.join(scratch, 'rules.d', '99-leetro-laser.rules')

    def run_root(target, udev_code=0):
        tool('udevadm', f'echo "$*" >> {log}\nexit {udev_code}\n')
        return subprocess.run(['/bin/sh', '-c', lua.root_script(target, bindir + ':/usr/bin:/bin', old)],
                              input=rule, capture_output=True)

    target = os.path.join(scratch, 'rules.d', '70-leetro-laser.rules')
    os.mkdir(os.path.dirname(target))
    with open(old, 'wb') as f:
        f.write(rule)                   # a computer set up before the rename: only the 99- file
    check(not lua.rule_installed(target), 'rule_installed() false with only the old 99- file (fix is offered)')
    r = run_root(target)
    check(r.returncode == 0, f'root script runs (exit {r.returncode} {r.stderr!r})')
    check(open(target, 'rb').read() == rule, 'installed rule equals linux/70-leetro-laser.rules byte for byte')
    check(not os.path.exists(old), 'the old 99- file is removed')
    check(stat.S_IMODE(os.stat(target).st_mode) == 0o644, 'installed rule is mode 0644')
    check(not os.path.exists(target + '.new'), 'no temporary file left')
    check(open(log).read().splitlines() == ['control --reload-rules',
                                            'trigger --subsystem-match=usb --attr-match=idVendor=0548'],
          'udev reloads its rules, then re-applies them to the laser')
    check(lua.rule_installed(target), 'rule_installed() sees it')
    with open(target, 'w') as f:
        f.write('old rule\n')
    r = run_root(target)
    check(r.returncode == 0 and open(target, 'rb').read() == rule, 'an outdated 70- file is replaced (no old 99- file: fine)')
    check(not lua.rule_installed(os.path.join(scratch, 'none')), 'rule_installed() false when missing')
    check(run_root(target, udev_code=3).returncode == 1, 'a failing step exits 1 (never 126/127)')
    r = subprocess.run(['/bin/sh', '-c', lua.root_script(os.path.join(scratch, 'no', 'dir', 'x'), bindir)],
                       input=rule, capture_output=True)
    check(r.returncode == 1, 'unwritable target exits 1')

    # ------------------------------------------------------------ install_rule with a fake pkexec
    got = os.path.join(scratch, 'pkexec')
    tool('pkexec', f'printf "%s\\n" "$0" "$@" > {got}.argv\n'
                   f'cat > {got}.stdin\nenv > {got}.env\n'
                   'echo "$FAKE_ERR" >&2\nexit "$FAKE_CODE"\n')
    pk = os.path.join(bindir, 'pkexec')

    def install(code, err='', **extra):
        env = dict(PATH=bindir + ':/usr/bin:/bin', FAKE_CODE=str(code), FAKE_ERR=err, **extra)
        waited = []
        result = lua.install_rule(wait=lambda done: waited.append(done()), env=env)
        return result, waited

    (ok, msg), waited = install(0)
    check(ok and msg == '', 'pkexec exit 0 is success')
    check(open(got + '.argv').read().splitlines() == lua.pkexec_argv(pk),
          'pkexec gets exactly [pkexec, /bin/sh, -c, script]')
    check(open(got + '.stdin', 'rb').read() == rule, 'the rule reaches it on stdin, byte for byte')
    check(len(waited) == 1, 'the window is kept drawn while the password prompt is up')
    (ok, msg), _ = install(126)
    check(not ok and 'Nothing was changed' in msg and 'cancelled' in msg, f'cancel (126): {msg!r}')
    (ok, msg), _ = install(127, 'Error executing command as another user: No authentication agent found.')
    check(not ok and 'Nothing was changed' in msg and 'No authentication agent' in msg and 'terminal' in msg,
          'refused (127): nothing changed, pkexec\'s reason and the terminal command')
    (ok, msg), _ = install(1, 'udevadm: boom')
    check(not ok and 'Could not allow it' in msg and 'udevadm: boom' in msg and 'terminal' in msg,
          'failure: shows stderr and the terminal command')
    with Patch((lua, 'find_pkexec', lambda env: None)):
        ok, msg = lua.install_rule(env={'PATH': bindir})
    check(not ok and 'no pkexec' in msg and 'install-udev.sh' in msg, 'no pkexec: says so, gives the terminal command')
    check(lua.find_pkexec({'PATH': bindir}) == pk, 'pkexec is found by absolute path')

    # the app's environment: the bundle's library path must not reach pkexec
    meipass = os.path.join(scratch, 'bundle')
    os.makedirs(os.path.join(meipass, 'linux'))
    shutil.copy(RULE, os.path.join(meipass, 'linux'))
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get('PATH', ''), FAKE_CODE='0', FAKE_ERR='',
               LD_LIBRARY_PATH='/bundle/lib', LD_LIBRARY_PATH_ORIG='/usr/lib/orig', _PYI_X='1', TCL_LIBRARY='/b/tcl')
    with Patch((sys, 'frozen', True), (sys, '_MEIPASS', meipass), (os, 'environ', env)):
        ok, msg = lua.install_rule()
        cmd = lua.terminal_command()
    seen = dict(line.split('=', 1) for line in open(got + '.env').read().splitlines() if '=' in line)
    check(ok and seen.get('LD_LIBRARY_PATH') == '/usr/lib/orig' and '_PYI_X' not in seen
          and 'TCL_LIBRARY' not in seen and 'LD_LIBRARY_PATH_ORIG' not in seen,
          "in the app, pkexec runs with the system's environment (system_env)")
    check(open(got + '.stdin', 'rb').read() == rule, 'in the app, the bundled rule is the one sent')
    printf = cmd.split(' | sudo tee ')[0]
    out = subprocess.run(['/bin/sh', '-c', printf], capture_output=True).stdout
    check(out == rule and cmd.endswith(f'sudo rm -f {lua.OLD_TARGET} && sudo {lua.RELOAD} && sudo {lua.TRIGGER}')
          and f'sudo tee {lua.TARGET} ' in cmd,
          'the app\'s terminal fallback writes the same rule byte for byte')
finally:
    shutil.rmtree(scratch)

# ---------------------------------------------------------------- the window's dialogs
try:
    import laser_window
except (ImportError, SystemExit) as e:
    print(f'skip  window dialogs ({e}; run with the project venv)')
    laser_window = None
if laser_window:
    def flow(installed, yes, result, retry=True):
        shown = []
        mb = laser_window.messagebox
        with Patch((lua, 'rule_installed', lambda: installed), (lua, 'install_rule', lambda wait=None: result),
                   (mb, 'askyesno', lambda t, m, **k: shown.append(('ask', m)) or yes),
                   (mb, 'showerror', lambda t, m, **k: shown.append(('error', m))),
                   (mb, 'askretrycancel', lambda t, m, **k: shown.append(('retry', m)) or retry)):
            again = laser_window.LaserWindow.allow_usb(types.SimpleNamespace(root=None))
        return again, shown

    again, shown = flow(False, True, (True, ''))
    check(again and [k for k, _ in shown] == ['ask', 'retry'] and "isn't allowed" in shown[0][1]
          and 'password' in shown[0][1] and 'plug it back in' in shown[1][1],
          'not set up: asks, installs, then says replug and offers Retry')
    again, shown = flow(False, False, None)
    check(not again and [k for k, _ in shown] == ['ask'], 'No: nothing else happens')
    again, shown = flow(False, True, (False, 'Nothing was changed (the password prompt was cancelled).'))
    check(not again and shown[-1] == ('error', 'Nothing was changed (the password prompt was cancelled).'),
          'install failed: its message is shown, no retry')
    again, shown = flow(True, True, None)
    check(not again and [k for k, _ in shown] == ['error'] and 'plug it back in' in shown[0][1],
          'already set up but still refused: says replug, asks nothing')

print()
if failures:
    sys.exit(f'{len(failures)} check(s) failed')
print('all checks passed')
