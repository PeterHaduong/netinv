"""Outputs: the inventory workbook and the feasibility report (designed to contain no
hostnames, IPs, serials, MACs or versions, so it can be reviewed and carried out)."""

import datetime
import re

from .kinds import KIND_LABELS, KIND_ORDER, MODULE_KINDS
from .history import seen_text
from .model import display_name, in_inventory, part_counts, part_identity
from .roles import role_label, role_name, valid_rules as valid_role_rules
from .kinds import valid_rules as valid_kind_rules
from . import VERSION

# --------------------------------------------------------------------------- workbook
#
# Two kinds of sheet:
#  * one grouped report sheet per network: a bold device row with its stack members /
#    modules / FEX / PSUs / fans grouped underneath (collapse with the 1/2 buttons or -/+),
#    thick outline per device. Reading/printing view - no sort buttons, so it can't be scrambled.
#  * Excel Tables (Devices, Hardware, Part counts, Issues, Changes) with a Network column:
#    sort and filter freely.
# Devices and parts without a serial number are left out; the Issues sheet says which.
# Devices SolarWinds did not reach this time show their last known hardware, greyed.

INVENTORY_HEADER = ['Device', 'Unit', 'Kind', 'Model / PID', 'Serial', 'Uplink module',
                    'Uplink module serial', 'Description', 'Software', 'Status / role', 'Mgmt IP',
                    'Site', 'SNMP location', 'Platform', 'Uptime', 'Notes']
PART_KINDS = ('supervisor', 'linecard', 'fabric', 'sysctrl', 'module', 'fex', 'power', 'fan',
              'transceiver', 'builtin', 'stack_cable', 'storage', 'acknowledged', 'other')
# Listed under a stack too (its members and uplink modules are listed anyway)
STACK_EXTRA_KINDS = ('stack_cable', 'storage', 'acknowledged', 'other')
# Part counts columns from settings "part_metadata" ({PID: {...}}), edited on the Part counts tab
EOL_COLUMNS = (('vuln_support', 'End of Vulnerability Support'), ('bulletin_id', 'EOL Bulletin ID'))
_DEVICE_FILL = 'DDEBF7'
_STALE_FILL = 'E7E6E6'


def unit_name(item):
    m = re.match(r'(?i)^slot\s*(\d+)', item['name'])
    if m:
        return 'Slot ' + m.group(1)
    if item['kind'] == 'fex':
        return item['member'] or item['name']
    return item['name']


def part_order(item):
    """Cards in slot order first, then FEXes, then power and fans."""
    group = {'power': 2, 'fan': 3, 'fex': 1}.get(item['kind'], 0)
    m = re.match(r'(?i)^slot\s*(\d+)', item['name'])
    return (group, int(m.group(1)) if m else 999, item['name'])


def device_kind(d):
    hw, members = d['hardware'], d['members']
    if d.get('stock'):
        return 'Stock'
    if d.get('vdc_of'):
        return 'VDC of %s' % d['vdc_of']
    if 'collection error' in d['commands']:
        return 'Not collected'
    if len(members) > 1:
        return 'Stack of %d' % len(members)
    if any(it['kind'] == 'chassis' for it in hw):
        kind = 'Chassis' if any(it['kind'] in ('supervisor', 'linecard') for it in hw) else 'Fixed'
        return kind + (' (VDCs: %s)' % ', '.join(d['vdcs']) if d.get('vdcs') else '')
    return 'Switch' if members else 'Device'


def device_source(d):
    if d.get('source') == 'manual':
        return 'Manual sheet'
    return 'SolarWinds (not reached since %s)' % d['last_seen'] if d.get('stale') else 'SolarWinds'


def device_parts(d):
    """The rows under a device: its stack members (with their uplink modules), or its cards,
    FEXes, power supplies and fans. -> [(unit, kind label, item, uplink modules or None)]"""
    hw = d['hardware']
    if d.get('stock'):                   # spares on a shelf: everything, by kind and PID
        order = dict((k, i) for i, k in enumerate(KIND_ORDER))
        return [(it['name'], KIND_LABELS.get(it['kind'], it['kind']), it, None)
                for it in sorted(hw, key=lambda it: (order.get(it['kind'], 99), it['pid'], it['serial']))]
    if d['members']:
        out = []
        for m in sorted(d['members'], key=lambda m: int(m['slot']) if m['slot'].isdigit() else 0):
            uplinks = [it for it in hw if it['kind'] == 'module' and it['member'] == m['slot']]
            out.append(('%s(%s)' % (d['hostname'], m['slot']),
                        'Stack member' + (' (%s)' % m['role'] if m['role'] else ''), m, uplinks))
        # stack cables, disks, acknowledged and unrecognised parts: listed, so they can be seen / named
        out += [(unit_name(it), KIND_LABELS[it['kind']], it, None)
                for it in sorted((it for it in hw if it['kind'] in STACK_EXTRA_KINDS), key=part_order)]
        return out
    return [(unit_name(it), KIND_LABELS.get(it['kind'], it['kind']), it, None)
            for it in sorted((it for it in hw if it['kind'] in PART_KINDS), key=part_order)]


def inventory_sheet(network, devices):
    rows, styles, blocks, outline = [], {}, [], {}
    for d in sorted((d for d in devices if in_inventory(d)), key=lambda d: (d['site'], display_name(d))):
        host = display_name(d)
        first = len(rows)
        styles[first] = {'bold': True, 'fill': _STALE_FILL if d.get('stale') else _DEVICE_FILL}
        version = d['version'] + (' (%s)' % d['install_mode'] if d['install_mode'] else '')
        rows.append([host, '', device_kind(d), d['model'], d['serial'], '', '', '', version, '',
                     d['mgmt_ip'], d['site'], d['location'], d['platform'], d['uptime'],
                     '; '.join(d['warnings'])])
        for unit, kind, it, uplinks in device_parts(d):
            if uplinks is not None:
                rows.append([host, unit, kind, it['pid'], it['serial'],
                             ', '.join(u['pid'] for u in uplinks) or 'none',
                             ', '.join(u['serial'] for u in uplinks), it['descr'], it['sw'],
                             it['status'], '', '', '', '', '', ''])
            else:
                status = ' / '.join(x for x in (it['status'], it['role']) if x)
                rows.append([host, unit, kind, it['pid'], it['serial'], '', '', it['descr'], it['sw'],
                             status, '', '', it.get('location', ''), '', '', it.get('notes', '')])
        for r in range(first + 1, len(rows)):
            outline[r] = 1
        blocks.append((first, len(rows) - 1))
    return (network, INVENTORY_HEADER, rows,
            {'row_styles': styles, 'blocks': blocks, 'outline': outline, 'autofilter': False,
             'wrap': {INVENTORY_HEADER.index('Description'): 40, INVENTORY_HEADER.index('Notes'): 60}})


def serial_sheet(networks):
    """One row per unique serial number (the deduplicated list): what it is and where it was seen.
    A part listed under two devices - a FEX attached to two leafs - is one row naming both."""
    order = dict((k, i) for i, k in enumerate(KIND_ORDER))
    seen = {}
    for name, devices in networks:
        for d in devices:
            for it in d['hardware']:
                if not it['serial']:
                    continue
                r = seen.setdefault(it['serial'], {'it': it, 'nets': [], 'devs': [], 'parts': set(), 'ack': False})
                r['ack'] = r['ack'] or bool(it.get('shared_serial'))
                if name not in r['nets']:
                    r['nets'].append(name)
                if display_name(d) not in r['devs']:
                    r['devs'].append(display_name(d))
                r['parts'].add(part_identity(d, it))
    rows = [[sn, r['it']['pid'], KIND_LABELS.get(r['it']['kind'], r['it']['kind']), r['it']['descr'],
             ', '.join(r['nets']), ', '.join(r['devs']), len(r['devs']),
             _serial_note(r)]
            for sn, r in sorted(seen.items(), key=lambda kv: (order.get(kv[1]['it']['kind'], 99),
                                                              kv[1]['it']['pid'], kv[0]))]
    return ('Serial numbers', ['Serial', 'PID', 'Kind', 'Description', 'Network', 'Device', 'Devices', 'Note'],
            rows, {'table': 'SerialNumbers', 'wrap': {3: 40, 5: 50}})


def _serial_note(r):
    if r['ack']:
        return 'Shared / default serial (acknowledged): %d parts' % len(r['parts'])
    if len(r['parts']) > 1:
        return 'Same serial on %d different parts (default serial?)' % len(r['parts'])
    return ''


def part_count_sheet(networks, part_metadata=None):
    """Unique part numbers with how many there are: the EOL columns (settings "part_metadata",
    edited on the app's Part counts tab), a Total column, then one per network."""
    part_metadata = part_metadata if isinstance(part_metadata, dict) else {}
    names = [name for name, _ in networks]
    rows = []
    for r in part_counts(networks):
        meta = part_metadata.get(r['pid']) or {}
        rows.append([KIND_LABELS.get(r['kind'], r['kind']), r['pid'], r['descr']]
                    + [meta.get(k, '') if isinstance(meta, dict) else '' for k, _ in EOL_COLUMNS]
                    + [r['total']] + [r['counts'].get(n, 0) for n in names])
    return ('Part counts', ['Kind', 'PID', 'Description'] + [h for _, h in EOL_COLUMNS] + ['Total'] + names,
            rows, {'table': 'PartCounts', 'wrap': {2: 45}})


def workbook_sheets(devices, network='Inventory'):
    """Workbook for one network."""
    return workbook_for_networks([(network, devices)])


def excluded_note(networks):
    """'Not counted: 30 devices in NET-A and 2 in NET-B, left out by exclusion rules.' or ''
    when nothing is excluded. Counts only - with dozens of exclusions, names would be a wall
    of text; they are in the Excluded branch and the excluded-devices workbook.
    networks: [(name, excluded devices)]."""
    parts = [(len(devices), name) for name, devices in networks if devices]
    if not parts:
        return ''
    text = ['%d device%s in %s' % (parts[0][0], '' if parts[0][0] == 1 else 's', parts[0][1])]
    text += ['%d in %s' % p for p in parts[1:]]
    joined = text[0] if len(text) == 1 else ', '.join(text[:-1]) + ' and ' + text[-1]
    return 'Not counted: %s, left out by exclusion rules.' % joined


def about_sheet(snaps, include_optics=False, generated=None, shared_serials=None, kind_rules=None,
                role_rules=None):
    """The 'About' sheet that opens the inventory workbook: what it covers and how parts are
    counted, so a reader does not have to guess. snaps: [(network, snapshot as loaded)];
    shared_serials: the acknowledged shared / default serials; kind_rules / role_rules: the
    local rules from settings.json, so a reader knows which labels came from them."""
    generated = generated or datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = [['Generated', '%s (NetInv %s)' % (generated, VERSION)]]
    for name, snap in snaps:
        devices = [d for d in snap['devices'] if in_inventory(d)]
        rows.append(['Network %s' % name, '%d devices, data imported %s'
                     % (len(devices), (snap.get('created') or '')[:16])])
    rows += [
        ['What is counted', 'Hardware with a serial number: chassis, stack members, FEX, supervisors, '
                            'line cards, modules, power supplies, fans. Parts without a serial (empty '
                            'slots, stack members provisioned but not installed, fans reporting N/A) '
                            'are left out and listed on the Issues sheet.'],
        ['Counted once', 'Part counts count each serial number once: a FEX attached to two switches, '
                         'or a Nexus 7700 split into VDCs, is one part. Parts built into a chassis '
                         '(Kind: Built-in) are listed but not counted separately.'],
        ['Optics', 'Optics (SFPs) are included.' if include_optics else
                   'Optics (SFPs) are not listed or counted.'],
    ]
    if shared_serials:
        rows.append(['Shared serials', 'These serial numbers are factory default serials reported by '
                     'different parts, so each part reporting one is counted separately: %s.'
                     % ', '.join(sorted(shared_serials))])
    kind_rules, role_rules = valid_kind_rules(kind_rules), valid_role_rules(role_rules)
    if kind_rules:
        rows.append(['Local part rules', 'Kinds set by local rules (not built into NetInv): ' + '; '.join(
            '%s matching %s -> %s' % ({'pid': 'PID', 'descr': 'description', 'any': 'name or description'}
                                      .get(r[1], r[1]), r[2], KIND_LABELS.get(r[0], r[0]))
            for r in kind_rules) + '.'])
    if role_rules:
        rows.append(['Local role rules', 'Roles set by local rules from the model, when the hostname '
                     'does not give one: ' + '; '.join('model matching %s -> %s' % (r[1], role_name(r[0]))
                                                      for r in role_rules) + '.'])
    stale = [(name, d) for name, snap in snaps for d in snap['devices'] if d.get('stale')]
    if stale:
        rows.append(['Not reached', '%d device%s could not be reached by SolarWinds this round; the '
                     'hardware shown for them is as last seen (Devices sheet: Source, Last reached).'
                     % (len(stale), '' if len(stale) == 1 else 's')])
    note = excluded_note([(name, snap.get('excluded', [])) for name, snap in snaps])
    if note:
        rows.append(['Not included', note + ' They are in no sheet or count of this workbook; a '
                     'separate excluded-devices workbook lists them.'])
    return ('About', ['Item', 'Details'], rows, {'table': 'About', 'wrap': {1: 100}})


def workbook_for_networks(networks, changes=None, baselines=None, about=None, part_metadata=None):
    """networks: [(name, devices)] -> sheets for write_xlsx: one grouped report sheet per
    network, then shared sortable tables with a Network column. Devices left out by exclusion
    rules are not passed in, so they appear nowhere here (see excluded_workbook).
    changes: optional {network: [(change, hostname, detail)]}; baselines: {network: date of the
    snapshot they were compared with}; about: optional About sheet (about_sheet) to open with;
    part_metadata: settings "part_metadata" for the Part counts EOL columns."""
    sheets = ([about] if about else []) + [inventory_sheet(name, devs) for name, devs in networks]
    dev_rows, hw_rows, issue_rows = [], [], []
    for name, devices in networks:
        for d in devices:
            for w in d['warnings']:
                issue_rows.append([name, display_name(d), w])
            if not in_inventory(d):
                continue
            kinds = {}
            for it in d['hardware']:
                kinds[it['kind']] = kinds.get(it['kind'], 0) + 1
            dev_rows.append([name, d['site'], display_name(d), role_label(d['hostname'], d['model']),
                             device_kind(d), device_source(d), d.get('last_seen', ''),
                             d['mgmt_ip'], d['platform'], d['model'], d['serial'],
                             d['version'], d['install_mode'], len(d['members']) or '',
                             kinds.get('fex', '') or '', kinds.get('supervisor', '') or '',
                             kinds.get('linecard', '') or '',
                             sum(kinds.get(k, 0) for k in MODULE_KINDS) or '',
                             d['location'], d['uptime'], d['reload_reason'], len(d['warnings']) or ''])
            for it in d['hardware']:
                unit = ('%s(%s)' % (d['hostname'], it['slot']) if it['kind'] == 'stack_member'
                        else ('%s(%s)' % (d['hostname'], it['member']) if it['member'].isdigit()
                              else unit_name(it)))
                hw_rows.append([name, it.get('location') or d['site'] or d['location'], display_name(d), unit,
                                KIND_LABELS.get(it['kind'], it['kind']), it['name'], it['descr'],
                                it['pid'], it['vid'], it['serial'], it['status'], it['role'], it['sw'],
                                it.get('first_seen', ''), it.get('last_seen', ''), seen_text(it),
                                '; '.join(x for x in (it.get('notes', ''), 'Shared / default serial (acknowledged)'
                                                      if it.get('shared_serial') else '') if x)])
    sheets += [
        ('Devices', ['Network', 'Site', 'Hostname', 'Role', 'Kind', 'Source', 'Last reached',
                     'Mgmt IP', 'Platform', 'Model',
                     'Serial', 'Software', 'Boot mode', 'Stack members', 'FEXes', 'Supervisors',
                     'Line cards', 'Modules', 'SNMP location', 'Uptime', 'Last reload reason',
                     'Issues'], dev_rows, {'table': 'Devices'}),
        ('Hardware', ['Network', 'Site / location', 'Hostname', 'Unit', 'Kind', 'Inventory name',
                      'Description', 'PID', 'VID', 'Serial', 'Status', 'Role / stock status', 'Software',
                      'First seen', 'Last seen', 'Seen', 'Notes'],
         hw_rows, {'table': 'Hardware', 'wrap': {6: 40, 16: 40}}),
        serial_sheet(networks),
        part_count_sheet(networks, part_metadata),
        ('Issues', ['Network', 'Hostname', 'Issue'], issue_rows, {'table': 'Issues', 'wrap': {2: 90}}),
    ]
    if changes is not None:
        since = baselines or {}
        change_rows = [[n, since.get(n, ''), c, h, det] for n in sorted(changes) for c, h, det in changes[n]]
        sheets.append(('Changes', ['Network', 'Compared with', 'Change', 'Hostname', 'Detail'], change_rows,
                       {'table': 'Changes', 'wrap': {4: 70}}))
    return sheets


EXCLUDED_SUFFIX = ' (excluded)'


def excluded_workbook(networks, part_metadata=None):
    """The separate workbook of devices left out by exclusion rules (e.g. another team's gear on
    a shared SolarWinds). networks: [(name, excluded devices)]. Same layout as the inventory
    workbook - every network name marked '(excluded)' so a copied sheet cannot be mistaken for
    inventory - after an 'Exclusion rules' sheet naming the rule that caught each device.
    These devices are in no count of the inventory workbook."""
    marked = [(name + EXCLUDED_SUFFIX, devices) for name, devices in networks if devices]
    rules = [[name, display_name(d), d['model'], d['serial'], d.get('excluded_by', '')]
             for name, devices in marked for d in sorted(devices, key=display_name)]
    about = ('About', ['Item', 'Details'], [
        ['Generated', '%s (NetInv %s)' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), VERSION)],
        ['What this is', 'Devices LEFT OUT of the inventory workbook by exclusion rules (e.g. another '
                         "team's equipment on a shared SolarWinds). None of these are counted in the "
                         'inventory workbook. The Exclusion rules sheet names the rule for each device.']],
        {'table': 'About', 'wrap': {1: 100}})
    return ([about, ('Exclusion rules', ['Network', 'Device', 'Model', 'Serial', 'Excluded by rule'], rules,
                     {'table': 'ExclusionRules'})] + workbook_for_networks(marked, part_metadata=part_metadata))


# --------------------------------------------------------------------------- masking

_KEEP = set('''
cisco ios xe software version nx-os nxos nexus operating system uptime is name descr pid vid sn
switch ports model serial number chassis slot module supervisor power supply fan tray system
image file processor board id mod status ok active standby ha-standby device interface platform
capabilities port outgoing holdtime address addresses entry mgmt management native vlan duplex
full half local intf capability enabled time remaining seconds bios kernel last reload reason
reset hardware hw sw fw mac to card type mode install bundle rom bootldr router operating
autonomous configuration register stack linecard line fabric controller sec uptime: version:
the of for this by and with copyright technical support compiled release rommon memory bytes
physical flash total entries displayed displayed: series sup lc fm sc built-in nim subslot
transceiver redundancy role sso rpr none ip ipv4 ethernet gigabitethernet tengigabitethernet
'''.split())


def mask_line(line):
    """Keep known keywords and punctuation; reduce everything else to its 'shape'
    (letters -> A/a, digits -> 9). IPs and MACs become <ip>/<mac>."""
    line = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '<ip>', line)
    line = re.sub(r'\b[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\b', '<mac>', line)
    line = re.sub(r'\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b', '<mac>', line)

    def shape(m):
        tok = m.group(0)
        if tok.lower().strip(':,') in _KEEP or tok in ('<ip>', '<mac>'):
            return tok
        return re.sub(r'[0-9]', '9', re.sub(r'[a-z]', 'a', re.sub(r'[A-Z]', 'A', tok)))
    return re.sub(r'<ip>|<mac>|[A-Za-z0-9][A-Za-z0-9_\-]*:?', shape, line)[:120]


# --------------------------------------------------------------------------- report

def feasibility_report(env, split_stats, build_stats, devices, nodes_info, outputs, show_pids=True):
    out = []
    w = out.append
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    w('NETINV FEASIBILITY REPORT   (kit %s, %s)' % (VERSION, now))
    w('This report is built to contain NO hostnames, IP addresses, serial numbers, MAC addresses')
    w('or software versions. It does list Cisco part numbers (PIDs) the kit did not recognise%s.'
      % ('' if show_pids else ' (suppressed)'))
    w('Read it through and follow your own handling rules before moving it anywhere.')
    w('')
    w('[1] ENVIRONMENT')
    for k, v in env:
        w('  %-28s %s' % (k, v))
    w('')
    if split_stats is None:
        w('[2] INPUT: not analysed (no folder chosen)')
        return '\n'.join(out)

    w('[2] INPUT STRUCTURE')
    w('  Files seen / used / skipped:  %d / %d / %d   (%.1f MB read)'
      % (split_stats['files_seen'], split_stats['files_used'], split_stats['files_skipped'],
         split_stats['bytes'] / 1048576.0))
    for reason, n in sorted(split_stats['skipped_reasons'].items()):
        w('    skipped, %s: %d' % (reason, n))
    w('  File layouts found:')
    for layout, n in sorted(split_stats['layouts'].items(), key=lambda kv: str(kv[0])):
        w('    %-34s %d files' % (layout, n))
    w('  Command sections found:')
    for cmd, n in sorted(build_stats['sections'].items()):
        unsup = build_stats['unsupported'].get(cmd, 0)
        w('    %-12s %5d%s' % (cmd, n, ('   (%d said "invalid command")' % unsup) if unsup else ''))
    sources = {}
    for d in devices:
        for s in d['host_sources']:
            sources[s] = sources.get(s, 0) + 1
    w('  Devices: %d.  Where the device name came from:' % len(devices))
    for s, n in sorted(sources.items()):
        w('    %-38s %d' % (s, n))
    if build_stats['unidentified_sections']:
        w('  !! %d sections could not be tied to a device name' % build_stats['unidentified_sections'])
    if nodes_info:
        w('  Node list CSV: %d rows; columns used: host=%s ip=%s site=%s'
          % (nodes_info['rows'], nodes_info['host_column'], nodes_info['ip_column'],
             nodes_info['site_column']))
        w('    devices matched to node list: %d of %d'
          % (sum(1 for d in devices if d['mgmt_ip']), len(devices)))
    w('')

    w('[3] PARSE RESULTS')
    plat = {}
    for d in devices:
        plat[d['platform'] or 'unknown'] = plat.get(d['platform'] or 'unknown', 0) + 1
    w('  Platforms: ' + ', '.join('%s %d' % kv for kv in sorted(plat.items())))
    w('  Software version found:  %d of %d devices with show version'
      % (sum(1 for d in devices if d['version']),
         sum(1 for d in devices if 'version' in d['commands'])))
    stacks = [d for d in devices if len(d['members']) > 1]
    w('  Stacks: %d (sizes: %s)' % (len(stacks), ', '.join(sorted({str(len(d['members'])) for d in stacks})) or '-'))
    w('  Inventory entries: %d read of %d NAME lines'
      % (build_stats['inventory_items'], build_stats['inventory_name_lines']))
    for kind, n in sorted(build_stats['kinds'].items(), key=lambda kv: -kv[1]):
        w('    %-30s %6d%s' % (KIND_LABELS.get(kind, kind), n,
                                {'transceiver': '   (hidden unless optics are on)',
                                 'stack': '   (excluded)'}.get(kind, '')))
    if build_stats['module_unparsed']:
        w('  show module rows not understood: %d' % build_stats['module_unparsed'])
    w('  Devices in the inventory (with a serial number): %d of %d'
      % (sum(1 for d in devices if in_inventory(d)), len(devices)))
    if build_stats.get('no_serial'):
        w('  Parts left out (no serial number): ' + ', '.join(
            '%s %d' % (KIND_LABELS.get(k, k), n) for k, n in sorted(build_stats['no_serial'].items())))
    warn_kinds = {}
    for d in devices:
        for msg in d['warnings']:
            generic = re.sub(r'\S*\d\S*', '#', msg)
            generic = re.sub(r'(says|appears on|Prompt says) .*', r'\1 ...', generic)
            warn_kinds[generic] = warn_kinds.get(generic, 0) + 1
    if warn_kinds:
        w('  Warnings (generalised):')
        for msg, n in sorted(warn_kinds.items(), key=lambda kv: -kv[1])[:25]:
            w('    %4d x %s' % (n, msg))
    w('')

    w('[4] WHAT THE KIT DID NOT UNDERSTAND')
    if build_stats['unrecognised'] and show_pids:
        w('  Inventory entries it did not recognise (PID | description):')
        for (pid, descr), n in sorted(build_stats['unrecognised'].items(), key=lambda kv: -kv[1])[:40]:
            w('    %4d x %-22s | %s' % (n, pid, descr))
    elif build_stats['unrecognised']:
        w('  %d distinct unrecognised part numbers (listing suppressed)' % len(build_stats['unrecognised']))
    else:
        w('  Every inventory entry was recognised.')
    if build_stats['version_missing_lines']:
        w('  show version sections with no version found (first lines, shape-masked):')
        for block in build_stats['version_missing_lines'][:3]:
            for line in block:
                w('    | ' + mask_line(line))
            w('    ---')
    w('')
    w('[5] FILES WRITTEN (these contain real data - keep them on this network)')
    for label, path in outputs:
        w('  %-26s %s' % (label, path))
    return '\n'.join(out)
