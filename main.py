#!/usr/bin/env python3
"""NetInv - network hardware inventory from SolarWinds show-command output.

Double-click (or `python main.py`) for the app. Standard library only, Python 3.6+.

Folders next to this file:
    netinv/           the program (do not edit to use the app)
    inventory_data/   your imports, settings and outputs - created on first run; back it up
    input/            put SolarWinds results and manual sheets here (the file pickers open it)

Command line (for scripting or a PC without a GUI):
    python main.py [--workspace DIR]                       start the app
    python main.py import NETWORK PATH [--nodes CSV]       parse SolarWinds exports into a snapshot
    python main.py import-manual SHEET [--network NAME]    import the manual inventory sheet
    python main.py manual-sheet OUT.xlsx [NETWORK ...]     blank manual sheet, or those networks' rows
    python main.py forget NETWORK HOSTNAME ...             drop devices that are gone for good
    python main.py exclude NETWORK [PATTERN ...] [--remove]  hide devices (e.g. another team's)
    python main.py shared-serial [SERIAL ...] [--remove]   serials known to be factory defaults
                                                           shared by different parts
    python main.py list                                    networks in the workspace (in order)
    python main.py workbook OUT.xlsx [NETWORK ...] [--excluded]   Excel workbook (default: all);
                                                           --excluded: only the excluded devices
    python main.py commands [--all]                        the SolarWinds command list
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from netinv.workspace import Workspace                   # noqa: E402
from netinv.commands import CATALOG, default_commands, script_text   # noqa: E402
from netinv.model import display_name, in_inventory      # noqa: E402

DEFAULT_WORKSPACE = os.path.join(HERE, 'inventory_data')
INPUT_DIR = os.path.join(HERE, 'input')
_OLD_WORKSPACE = os.path.join(HERE, 'netinv_data')        # name used before 0.2.x


def _prepare_folders(workspace):
    """Rename the old data folder (netinv_data) once, and make sure input/ exists."""
    if (workspace == DEFAULT_WORKSPACE and os.path.isdir(_OLD_WORKSPACE)
            and not os.path.exists(DEFAULT_WORKSPACE)):
        os.rename(_OLD_WORKSPACE, DEFAULT_WORKSPACE)
    if not os.path.isdir(INPUT_DIR):
        os.makedirs(INPUT_DIR)


def main(argv=None):
    ap = argparse.ArgumentParser(description='NetInv')
    ap.add_argument('--workspace', default=DEFAULT_WORKSPACE)
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('import')
    p.add_argument('network')
    p.add_argument('path')
    p.add_argument('--nodes')
    p = sub.add_parser('import-manual')
    p.add_argument('sheet')
    p.add_argument('--network', default='', help='network for rows whose Network column is blank')
    p = sub.add_parser('manual-sheet')
    p.add_argument('out')
    p.add_argument('networks', nargs='*')
    p = sub.add_parser('forget')
    p.add_argument('network')
    p.add_argument('hostnames', nargs='+')
    p = sub.add_parser('exclude', help='show / add / remove exclusion rules (hostname wildcards)')
    p.add_argument('network')
    p.add_argument('patterns', nargs='*')
    p.add_argument('--remove', action='store_true')
    p = sub.add_parser('shared-serial', help='show / add / remove acknowledged shared (default) serials')
    p.add_argument('serials', nargs='*')
    p.add_argument('--remove', action='store_true')
    sub.add_parser('list')
    p = sub.add_parser('workbook')
    p.add_argument('out')
    p.add_argument('networks', nargs='*')
    p.add_argument('--excluded', action='store_true',
                   help='write the separate workbook of devices left out by exclusion rules')
    p = sub.add_parser('commands')
    p.add_argument('--all', action='store_true', help='include the optional commands')
    args = ap.parse_args(argv)
    _prepare_folders(args.workspace)

    if args.cmd is None:
        from netinv.gui import run
        run(args.workspace, INPUT_DIR)
        return 0

    ws = Workspace(args.workspace)
    for problem in ws.use_rules():
        sys.stderr.write('settings.json: %s (ignored)\n' % problem)
    if args.cmd == 'commands':
        sys.stdout.write(script_text([c for c, _, _, _ in CATALOG] if args.all else default_commands()))
    elif args.cmd == 'import':
        snap = ws.import_exports(args.network, args.path, args.nodes)
        print('%s: %d devices (%d not reached, last known hardware kept; %d listed under two names, '
              'counted once - see Issues), %d issues'
              % (args.network, sum(1 for d in snap['devices'] if in_inventory(d)),
                 sum(1 for d in snap['devices'] if d.get('stale')),
                 snap['build_stats'].get('combined', 0),
                 sum(len(d['warnings']) for d in snap['devices'])))
    elif args.cmd == 'import-manual':
        snaps, info = ws.import_manual(args.sheet, args.network)
        print('%d of %d rows used' % (info['used'], info['rows']))
        for row, problem in info['problems']:
            print('  row %d: %s' % (row, problem))
        for n, snap in sorted(snaps.items()):
            print('%s: %d manual devices' % (n, sum(1 for d in snap['devices'] if d.get('source') == 'manual')))
    elif args.cmd == 'manual-sheet':
        from netinv.manual import template_sheets
        from netinv.xlsx import write_xlsx
        write_xlsx(args.out, template_sheets([(n, ws.load(n, raw=True)['devices']) for n in args.networks]))
        print('Wrote', args.out)
    elif args.cmd == 'forget':
        print('Forgot: %s' % (', '.join(ws.forget(args.network, args.hostnames)) or 'nothing'))
    elif args.cmd == 'exclude':
        rules = ws.exclusions(args.network)
        if args.patterns:
            rules = ([r for r in rules if r not in args.patterns] if args.remove
                     else rules + args.patterns)
            ws.set_exclusions(args.network, rules)
        snap = ws.load(args.network)
        print('Rules: %s' % (', '.join(ws.exclusions(args.network)) or 'none'))
        for d in snap.get('excluded', []):
            print('  excluded  %-30s by %s' % (display_name(d), d['excluded_by']))
    elif args.cmd == 'shared-serial':
        serials = ws.shared_serials()
        if args.serials:
            serials = ([s for s in serials if s not in args.serials] if args.remove
                       else serials + args.serials)
            ws.set_shared_serials(serials)
        print('Shared serials: %s' % (', '.join(ws.shared_serials()) or 'none'))
        for n in ws.networks():
            for d in ws.load(n)['devices']:
                for it in d['hardware']:
                    if it.get('shared_serial'):
                        print('  %-14s %-20s %-24s %s' % (it['serial'], n, display_name(d), it['name']))
    elif args.cmd == 'list':
        for n in ws.networks():
            s = ws.load(n)
            print('%-20s %4d devices  imported %s' % (n, sum(1 for d in s['devices'] if in_inventory(d)),
                                                      s['created']))
    elif args.cmd == 'workbook':
        from netinv.report import about_sheet, excluded_workbook, workbook_for_networks
        from netinv.xlsx import write_xlsx
        nets = args.networks or ws.networks()
        snaps = dict((n, ws.load(n)) for n in nets)
        if args.excluded:
            write_xlsx(args.out, excluded_workbook([(n, snaps[n]['excluded']) for n in nets],
                                                   ws.settings().get('part_metadata')))
        else:
            write_xlsx(args.out, workbook_for_networks(
                [(n, snaps[n]['devices']) for n in nets], part_metadata=ws.settings().get('part_metadata'),
                about=about_sheet([(n, snaps[n]) for n in nets], ws.include_optics(),
                                  shared_serials=ws.shared_serials(),
                                  kind_rules=ws.settings().get('kind_rules'),
                                  role_rules=ws.settings().get('role_rules'))))
        print('Wrote', args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
