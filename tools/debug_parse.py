#!/usr/bin/env python3
"""Show exactly what netinv extracted from some exports - for refining parsers.

    python tools/debug_parse.py <file or folder> [--device HOST] [--raw]
    python tools/debug_parse.py <folder> --save-fixture <name>

Default output: how each file was split (device, command, how recognised), then per device
the parsed fields, hardware, parts left out for having no serial number, and warnings. --raw also prints each command
section's text, so a section and its parse result can be pasted together into a chat.

--save-fixture copies the input into tests/fixtures_local/<name>/ and records today's
results as expected.json. After checking those results by eye, every later code change is
back-tested against them with:  python -m unittest discover -s tests
(fixtures_local holds real data - it stays where it was collected.)
"""

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from netinv.splitter import split_inputs                       # noqa: E402
from netinv.model import build_devices, display_name           # noqa: E402

FIXTURES = os.path.join(ROOT, 'tests', 'fixtures_local')


def summarise(devices):
    """The facts a fixture pins down (kept small so expected.json is easy to hand-edit)."""
    out = {'devices': {}}
    for d in devices:
        kinds = {}
        for h in d['hardware']:
            kinds[h['kind']] = kinds.get(h['kind'], 0) + 1
        out['devices'][d['hostname']] = {
            'platform': d['platform'], 'version': d['version'], 'model': d['model'],
            'serial': d['serial'], 'members': len(d['members']), 'kinds': kinds,
            'vdc_of': d['vdc_of'], 'no_serial': len(d['no_serial']), 'warnings': len(d['warnings'])}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('path')
    ap.add_argument('--device', help='only show this device')
    ap.add_argument('--raw', action='store_true', help='also print each section\'s raw text')
    ap.add_argument('--save-fixture', metavar='NAME')
    args = ap.parse_args(argv)

    sections, stats = split_inputs(args.path)
    devices, bstats = build_devices(sections)

    if args.save_fixture:
        dest = os.path.join(FIXTURES, args.save_fixture)
        if os.path.exists(dest):
            sys.exit('%s already exists - pick another name or delete it' % dest)
        inp = os.path.join(dest, 'input')
        if os.path.isdir(args.path):
            shutil.copytree(args.path, inp)
        else:
            os.makedirs(inp)
            shutil.copy2(args.path, inp)
        with open(os.path.join(dest, 'expected.json'), 'w') as fh:
            json.dump(summarise(devices), fh, indent=2, sort_keys=True)
        print('Saved fixture %s. Check expected.json by eye - it records what the tool says NOW.' % dest)
        return 0

    print('=== SPLIT: %d files, layouts %s' % (stats['files_seen'], stats['layouts']))
    for s in sections:
        print('  %-40s %-12s %-24s via %s/%s' % (os.path.relpath(s.path, args.path if os.path.isdir(args.path) else os.path.dirname(args.path))[:40],
                                                 s.cmd, s.host, s.how, s.host_source))
    for d in devices:
        if args.device and args.device.lower() not in (d['hostname'].lower(), display_name(d).lower()):
            continue
        print('\n=== DEVICE %s' % display_name(d))
        for k in ('platform', 'version', 'model', 'serial', 'install_mode', 'uptime',
                  'reload_reason', 'router_mode', 'location', 'vdc_of', 'vdcs'):
            if d.get(k):
                print('  %-14s %s' % (k, d[k]))
        print('  commands       %s' % ', '.join(sorted(d['commands'])))
        if d['hardware']:
            print('  hardware:')
            for h in d['hardware']:
                if h['kind'] == 'transceiver':
                    continue
                print('    %-13s %-6s %-30s %-20s %-14s %s %s' % (h['kind'], h['slot'] or h['member'],
                      h['name'][:30], h['pid'][:20], h['serial'], h['status'], h['role']))
        for h in d['no_serial']:
            print('  no serial (left out): %-13s %-30s %s' % (h['kind'], h['name'][:30], h['pid']))
        if d['optics']:
            print('  optics (not listed): %d' % d['optics'])
        for w in d['warnings']:
            print('  WARNING %s' % w)
        if args.raw:
            for s in sections:
                if s.host and s.host.lower().split('.')[0] == d['key']:
                    print('\n  --- raw %s (%s) ---\n%s' % (s.cmd, os.path.basename(s.path), s.text))
    if bstats['unrecognised']:
        print('=== UNRECOGNISED inventory entries (add a rule in netinv/kinds.py):')
        for (pid, descr), n in sorted(bstats['unrecognised'].items()):
            print('  %3d x %-22s %s' % (n, pid, descr))
    return 0


if __name__ == '__main__':
    sys.exit(main())
