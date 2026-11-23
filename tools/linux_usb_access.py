"""Linux only: let the logged-in user use the laser over USB, from Laser Prepare (no terminal).

Installs linux/70-leetro-laser.rules (the one copy of the rule; linux/install-udev.sh installs the
same file, and the AppImage bundles it) as /etc/udev/rules.d/70-leetro-laser.rules through pkexec,
so the desktop asks for the password, and removes the old 99-leetro-laser.rules (named too late:
systemd's 73-seat-late.rules only gives devices already tagged "uaccess" to the user). The root
command is a fixed script; the rule's bytes reach it on stdin, nothing from the user or the file
system is put into the command line.
"""
import os
import shlex
import shutil
import subprocess
import sys

RULE_NAME = '70-leetro-laser.rules'
TARGET = '/etc/udev/rules.d/' + RULE_NAME
OLD_TARGET = '/etc/udev/rules.d/99-leetro-laser.rules'
SYS_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
RELOAD = 'udevadm control --reload-rules'
TRIGGER = 'udevadm trigger --subsystem-match=usb --attr-match=idVendor=0548'
CANCELLED, NOT_AUTHORIZED = 126, 127        # pkexec's own exit codes; the script itself exits 0 or 1


def rule_path():
    """linux/70-leetro-laser.rules: bundled in the app (PyInstaller), else in the repo."""
    if getattr(sys, 'frozen', False):
        return os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(sys.executable)), 'linux', RULE_NAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'linux', RULE_NAME)


def rule_bytes():
    with open(rule_path(), 'rb') as f:
        return f.read()


def rule_installed(target=TARGET):
    """True if the installed rule is this one (then a refusal needs a replug, not a new rule). Only the
    70- file counts: a computer with just the old 99- file is offered the fix."""
    try:
        with open(target, 'rb') as f:
            return f.read() == rule_bytes()
    except OSError:
        return False


def root_script(target=TARGET, path=SYS_PATH, old=OLD_TARGET):
    """The shell script pkexec runs as root: the rule from stdin into a new file next to the target,
    then renamed over it (never half-written), the old 99- file removed, then udev reloads its rules
    and re-applies them to the laser. Fixed text: target, old and path are constants (parameters
    only for the tests)."""
    t, new = shlex.quote(target), shlex.quote(target + '.new')
    return (f'PATH={shlex.quote(path)}; umask 022; '
            f'cat > {new} && chmod 0644 {new} && mv -f {new} {t} && rm -f {shlex.quote(old)} '
            f'&& {RELOAD} && {TRIGGER} || exit 1')


def find_pkexec(env=None):
    env = os.environ if env is None else env
    found = shutil.which('pkexec', path=env.get('PATH') or SYS_PATH)
    if found:
        return os.path.abspath(found)
    return '/usr/bin/pkexec' if os.access('/usr/bin/pkexec', os.X_OK) else None


def pkexec_argv(pkexec):
    return [pkexec, '/bin/sh', '-c', root_script()]


def terminal_command():
    """What to type instead when pkexec can't be used."""
    script = os.path.join(os.path.dirname(rule_path()), 'install-udev.sh')
    if not getattr(sys, 'frozen', False) and os.path.exists(script):
        return shlex.quote(os.path.normpath(script))
    lines = rule_bytes().decode().splitlines()
    return (f"printf '%s\\n' {' '.join(shlex.quote(s) for s in lines)} | sudo tee {TARGET} > /dev/null"
            f' && sudo rm -f {OLD_TARGET} && sudo {RELOAD} && sudo {TRIGGER}')


def install_rule(wait=None, env=None):
    """Install the rule through pkexec: (ok, message for the user). wait(done) keeps the window
    drawn while the password dialog is up; without it this just blocks."""
    import file_dialogs
    env = file_dialogs.system_env() if env is None else env     # not the bundle's libraries
    fallback = f'You can do it in a terminal instead:\n\n{terminal_command()}\n\nthen unplug and replug the laser.'
    pkexec = find_pkexec(env)
    if pkexec is None:
        return False, f"Nothing was changed: this computer has no pkexec to ask for your password.\n\n{fallback}"
    data = rule_bytes()
    try:
        proc = subprocess.Popen(pkexec_argv(pkexec), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=env)
    except OSError as e:
        return False, f'Nothing was changed: could not run pkexec ({e}).\n\n{fallback}'
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except OSError:
        pass                        # pkexec refused before reading; its exit code says why
    if wait is not None:
        wait(lambda: proc.poll() is not None)
    proc.wait()
    err = proc.stderr.read().decode(errors='replace').strip()
    proc.stderr.close()
    if proc.returncode == 0:
        return True, ''
    if proc.returncode == CANCELLED:
        return False, 'Nothing was changed (the password prompt was cancelled).'
    detail = f'\n\n{err}' if err else ''
    if proc.returncode == NOT_AUTHORIZED:
        return False, f'Nothing was changed: the password was not accepted or could not be asked for.{detail}\n\n{fallback}'
    return False, f'Could not allow it.{detail}\n\n{fallback}'
