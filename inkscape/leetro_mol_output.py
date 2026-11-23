#!/usr/bin/env python3
"""Inkscape output extension: save the drawing as a Leetro LaserCut .MOL job.

Runs under Inkscape's own Python and hands the work to tools/svg2mol.py in this
project, using the project's virtual environment (which has svgelements).
"""
import os
import re
import subprocess
import sys
import tempfile

import inkex

HERE = os.path.dirname(os.path.realpath(__file__))
PROJECT = os.path.dirname(HERE)
SVG2MOL = os.path.join(PROJECT, 'tools', 'svg2mol.py')
VENV_PYTHON = os.path.join(PROJECT, '.venv', 'Scripts', 'python.exe') if os.name == 'nt' \
    else os.path.join(PROJECT, '.venv', 'bin', 'python')


class LeetroMolOutput(inkex.OutputExtension):
    def add_arguments(self, pars):
        pars.add_argument('--tab')
        pars.add_argument('--engrave_speed', type=float, default=500)
        pars.add_argument('--engrave_power', type=float, default=30)
        pars.add_argument('--engrave_interval', type=float, default=0.1)
        pars.add_argument('--engrave_bidir', type=inkex.Boolean, default=False)
        pars.add_argument('--cut_speed', type=float, default=10)
        pars.add_argument('--cut_power', type=float, default=30)
        pars.add_argument('--cut_corner', type=float, default=30)
        pars.add_argument('--settings_file', default='')
        pars.add_argument('--max_power', type=float, default=100)

    def load(self, stream):
        return None  # svg2mol reads the file itself

    def save(self, stream):
        o = self.options
        if not os.path.exists(VENV_PYTHON):
            raise inkex.AbortExtension(f'Project Python not found at {VENV_PYTHON}.\n'
                                       'Recreate it as described in DEVELOPMENT.md (Setting up a clone).')
        fd, out = tempfile.mkstemp(suffix='.MOL')
        os.close(fd)
        cmd = [VENV_PYTHON, SVG2MOL, o.input_file, '-o', out,
               '--default-engrave', f'speed={o.engrave_speed},power={o.engrave_power},'
                                    f'interval={o.engrave_interval},bidir={int(o.engrave_bidir)}',
               '--default-cut', f'speed={o.cut_speed},power={o.cut_power},corner={o.cut_corner}',
               '--max-power', str(o.max_power)]
        if o.settings_file:
            cmd += ['--settings', o.settings_file]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise inkex.AbortExtension('Could not convert to .MOL:\n' + (res.stderr or res.stdout).strip())
            with open(out, 'rb') as f:
                stream.write(f.read())
        finally:
            os.remove(out)
        # svg2mol's summary: first line is the input temp file, the rest describes the job
        summary = [line.strip() for line in res.stdout.splitlines()[1:] if not line.startswith('wrote')]
        est = re.search(r'~(\d+) s', res.stdout)
        msg = '\n'.join(summary)
        if est:
            secs = int(est.group(1))
            msg += f'\nEstimated time: {secs // 60} min {secs % 60} s'
        name = os.path.basename(getattr(o, 'output', '') or '') if isinstance(o.output, str) else ''
        stem = os.path.splitext(name)[0]
        if name and not re.fullmatch(r'[A-Za-z0-9_]{1,8}', stem):
            msg += (f'\n\nNote: "{name}" may not show on the machine. Use a name of up to 8 '
                    'letters or digits, e.g. SIGN1.MOL.')
        inkex.utils.errormsg(msg)


if __name__ == '__main__':
    LeetroMolOutput().run()
