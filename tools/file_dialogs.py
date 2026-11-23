"""File dialogs for the Laser Prepare window: the desktop's own picker on Linux, Tk's elsewhere.

askopenfilename / asksaveasfilename take the same arguments as tkinter.filedialog's and return the
same thing (the path, or '' when cancelled). On macOS (anything but Linux) they are tkinter's,
which shows the native panel there. On Linux, Tk's own dialog is poor, so they try in turn:

1. the XDG desktop portal (org.freedesktop.portal.FileChooser over the session D-Bus, through
   jeepney): GNOME's or KDE's picker, or the GTK one on Xfce with xdg-desktop-portal-gtk;
2. zenity, then kdialog, if installed;
3. Tk's dialog.

LASER_DIALOGS=portal|zenity|kdialog|tk in the environment forces one of them (for testing).
The window stays drawn while the picker is open, but ignores clicks (like a modal dialog).
"""
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from urllib.parse import unquote, urlparse


def askopenfilename(**options):
    if not sys.platform.startswith('linux'):
        return filedialog.askopenfilename(**options)
    return _linux(False, options)


def asksaveasfilename(**options):
    if not sys.platform.startswith('linux'):
        return filedialog.asksaveasfilename(**options)
    return _linux(True, options)


class Unavailable(Exception):
    """This kind of picker can't be shown here: try the next one."""


def _linux(save, options):
    pickers = {'portal': _portal, 'zenity': _zenity, 'kdialog': _kdialog}
    choice = os.environ.get('LASER_DIALOGS', '').strip().lower()
    order = [choice] if choice in pickers else [] if choice == 'tk' else list(pickers)
    root = options.get('parent') or tk._default_root
    for name in order:
        try:
            path = pickers[name](root, save, options)
        except Unavailable:
            continue
        except Exception as e:                      # a broken picker must not stop the user saving
            print(f'file_dialogs: {name} failed: {e!r}', file=sys.stderr)
            continue
        return _finish(root, save, options, path)
    ask = filedialog.asksaveasfilename if save else filedialog.askopenfilename
    return ask(**options)


def _finish(root, save, options, path):
    """Tk adds the default extension to a typed name without one; the native pickers don't."""
    if not path or not save:
        return path or ''
    ext = options.get('defaultextension') or ''
    if ext and not os.path.splitext(path)[1]:
        path += ext
        # the picker only checked the name as typed
        if os.path.exists(path) and not messagebox.askyesno(
                options.get('title') or 'Save', f'{os.path.basename(path)} already exists. Replace it?',
                parent=root):
            return ''
    return path


def _filters(options):
    """Tk's filetypes as [(label, ['*.svg', ...])]."""
    out = []
    for label, patterns in options.get('filetypes') or ():
        if isinstance(patterns, str):
            patterns = patterns.split()
        out.append((label, list(patterns)))
    return out


def _title(save, options):
    return options.get('title') or ('Save As' if save else 'Open')


def _wait(root, done):
    """Keep the window drawn (but not clickable) until done() is true. False if the window went away."""
    held = False
    if root is not None:
        try:
            root.tk.call('tk', 'busy', 'hold', root)
            held = True
        except tk.TclError:
            pass
    try:
        while not done():
            if root is not None:
                try:
                    root.update()
                except tk.TclError:                 # window closed meanwhile
                    return False
            time.sleep(0.02)
        return True
    finally:
        if held:
            try:
                root.tk.call('tk', 'busy', 'forget', root)
            except tk.TclError:
                pass


# ---------------------------------------------------------------- XDG desktop portal

PORTAL = ('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop', 'org.freedesktop.portal.FileChooser')


def _connect():
    from jeepney.io.blocking import open_dbus_connection
    return open_dbus_connection('SESSION')


def _parent_handle(root):
    """The window the picker belongs to, as the portal wants it ('' if unknown)."""
    try:
        if root.tk.call('tk', 'windowingsystem') == 'x11':
            return 'x11:%x' % int(root.wm_frame(), 16)
    except (tk.TclError, ValueError, AttributeError):
        pass
    return ''


def _portal_options(save, options):
    filters = [(label, [(0, p) for p in patterns]) for label, patterns in _filters(options)]
    out = {'modal': ('b', True)}
    if filters:
        out['filters'] = ('a(sa(us))', filters)
        out['current_filter'] = ('(sa(us))', filters[0])
    folder = options.get('initialdir')
    if folder and os.path.isdir(folder):
        out['current_folder'] = ('ay', os.fsencode(os.path.abspath(folder)) + b'\0')
    if save and options.get('initialfile'):
        out['current_name'] = ('s', options['initialfile'])
    return out


def _portal(root, save, options):
    try:
        from jeepney import DBusAddress, HeaderFields, MatchRule, message_bus, new_method_call
        from jeepney.wrappers import unwrap_msg
    except ImportError:
        raise Unavailable
    try:
        conn = _connect()
    except Exception:
        raise Unavailable                           # no session bus
    parent, title, opts = _parent_handle(root), _title(save, options), _portal_options(save, options)
    state = {}

    def work():
        try:
            token = 'laserprepare%d' % random.randrange(1 << 30)
            sender = conn.unique_name.lstrip(':').replace('.', '_')
            expected = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
            responses = MatchRule(type='signal', interface='org.freedesktop.portal.Request', member='Response')
            with conn.filter(responses, bufsize=8) as queue:
                # subscribe before asking, so a quick answer isn't missed
                watch = MatchRule(type='signal', interface='org.freedesktop.portal.Request', member='Response',
                                  path=expected)
                unwrap_msg(conn.send_and_get_reply(message_bus.AddMatch(watch), timeout=5))
                opts['handle_token'] = ('s', token)
                address = DBusAddress(PORTAL[1], bus_name=PORTAL[0], interface=PORTAL[2])
                call = new_method_call(address, 'SaveFile' if save else 'OpenFile', 'ssa{sv}', (parent, title, opts))
                handle = unwrap_msg(conn.send_and_get_reply(call, timeout=30))[0]
                if handle != expected:                  # portals before 0.9 chose their own path
                    unwrap_msg(conn.send_and_get_reply(message_bus.AddMatch(MatchRule(
                        type='signal', interface='org.freedesktop.portal.Request', member='Response',
                        path=handle)), timeout=5))
                state['shown'] = True
                while True:
                    msg = conn.recv_until_filtered(queue)
                    if msg.header.fields.get(HeaderFields.path) == handle:
                        state['answer'] = msg.body
                        return
        except Exception as e:
            state['error'] = e
        finally:
            conn.close()

    worker = threading.Thread(target=work, name='file-portal', daemon=True)
    worker.start()
    if not _wait(root, lambda: not worker.is_alive()):
        return ''
    if 'error' in state:
        if not state.get('shown'):
            raise Unavailable                       # no portal, or it has no file chooser
        print(f'file_dialogs: portal failed: {state["error"]!r}', file=sys.stderr)
        return ''
    code, results = state['answer']
    if code != 0:                                   # 1 cancelled, 2 closed some other way
        return ''
    uris = results.get('uris', ('as', []))[1]
    if not uris:
        return ''
    uri = urlparse(uris[0])
    return unquote(uri.path) if uri.scheme == 'file' else ''


# ---------------------------------------------------------------- zenity / kdialog

def system_env():
    """The environment the app was started with: the bundle (PyInstaller) points the library path at
    its own libraries, which crash system programs like zenity."""
    env = dict(os.environ)
    if getattr(sys, 'frozen', False):
        for var in ('LD_LIBRARY_PATH', 'LIBPATH'):
            orig = env.pop(var + '_ORIG', None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
        for var in ('TCL_LIBRARY', 'TK_LIBRARY'):   # set by PyInstaller's tkinter hook
            env.pop(var, None)
        for var in [v for v in env if v.startswith('_PYI_')]:
            env.pop(var)
    return env


def _run(root, args):
    """Run a picker program: the chosen path, '' when cancelled; Unavailable if it couldn't run."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=system_env())
    except OSError:
        raise Unavailable
    if not _wait(root, lambda: proc.poll() is not None):
        return ''
    out = proc.stdout.read().decode(errors='surrogateescape')
    proc.stdout.close()
    if proc.returncode == 1:
        return ''                                   # cancelled
    if proc.returncode != 0:
        raise Unavailable                           # e.g. no display it can use
    return out.rstrip('\n')


def _zenity(root, save, options):
    exe = shutil.which('zenity')
    if not exe:
        raise Unavailable
    folder = options.get('initialdir') or ''
    args = [exe, '--file-selection', '--title=' + _title(save, options)]
    if save:
        args += ['--save', '--confirm-overwrite']
        name = os.path.join(folder, options.get('initialfile') or '')
    else:
        name = os.path.join(folder, '') if folder else ''
    if name:
        args.append('--filename=' + name)
    for label, patterns in _filters(options):
        args.append(f'--file-filter={label} | {" ".join(patterns)}')
    return _run(root, args)


def _kdialog(root, save, options):
    exe = shutil.which('kdialog')
    if not exe:
        raise Unavailable
    folder = options.get('initialdir') or os.path.expanduser('~')
    start = os.path.join(folder, options.get('initialfile') or '') if save else folder
    filters = '\n'.join(f'{" ".join(patterns)}|{label}' for label, patterns in _filters(options))
    args = [exe, '--title', _title(save, options), '--getsavefilename' if save else '--getopenfilename', start]
    if filters:
        args.append(filters)
    return _run(root, args)
