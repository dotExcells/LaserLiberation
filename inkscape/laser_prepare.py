#!/usr/bin/env python3
"""Inkscape effect: open the "Prepare for laser" window for the current drawing.

The drawing isn't changed. The window (tools/laser_window.py) runs in the project's own
Python, which has Tk and svgelements; Inkscape waits until it is closed.
"""
import os
import shutil
import subprocess
import tempfile

import inkex

HERE = os.path.dirname(os.path.realpath(__file__))
PROJECT = os.path.dirname(HERE)
WINDOW = os.path.join(PROJECT, 'tools', 'laser_window.py')
VENV_PYTHON = os.path.join(PROJECT, '.venv', 'Scripts', 'python.exe') if os.name == 'nt' \
    else os.path.join(PROJECT, '.venv', 'bin', 'python')


class PrepareForLaser(inkex.EffectExtension):
    def effect(self):
        if not os.path.exists(VENV_PYTHON):
            raise inkex.AbortExtension(f'Project Python not found at {VENV_PYTHON}.\n'
                                       'Recreate it as described in DEVELOPMENT.md (Setting up a clone).')
        doc = self.document_path() or ''
        title = os.path.basename(doc) if doc else 'unsaved drawing'
        # Inkscape deletes its temp copy when we return; keep our own for the window
        tmpdir = tempfile.mkdtemp(prefix='laser_')
        svg = os.path.join(tmpdir, 'drawing.svg')
        with open(svg, 'wb') as f:
            self.document.write(f)
        try:
            res = subprocess.run([VENV_PYTHON, WINDOW, svg, '--title', title], capture_output=True, text=True)
            if res.returncode != 0:
                raise inkex.AbortExtension('The laser window stopped with an error:\n' + res.stderr[-2000:])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return False  # drawing unchanged


if __name__ == '__main__':
    PrepareForLaser().run()
