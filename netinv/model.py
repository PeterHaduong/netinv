"""Merge parsed sections into device records and hardware lists, and count parts.

Only hardware with a real serial number is kept: a provisioned-but-absent stack member, an
empty slot or a fan without a serial EEPROM is left out (and listed on the device's issues
when it is a chassis, stack member, FEX or module)."""

import csv
import fnmatch
import os
import re

from . import parsers
from .kinds import kind_of, slot_of, member_of, MODULE_KINDS, KIND_ORDER
from .textutil import device_key, is_ipv4, norm_host, read_text, real_serial, short_host, split_caption

OK_STATUS = re.compile(r'(?i)^(ok|active|standby|ha-standby|pass|ready)\b')

# Kinds whose missing serial is worth an issue line (fans / PSUs without one are routine).
_REPORT_UNSERIALIZED = ('chassis', 'stack_member', 'fex') + MODULE_KINDS


# --------------------------------------------------------------------------- node list CSV

_HOST_COLS = ('caption', 'nodename', 'node name', 'hostname', 'host name', 'sysname', 'name',
              'device', 'device name')
_IP_COLS = ('ip_address', 'ipaddress', 'ip address', 'ip', 'management ip', 'mgmt ip',
            'polling ip', 'ip_address_polling')
_SITE_COLS = ('site', 'building', 'campus', 'city')
_LOCATION_COLS = ('location', 'snmp location', 'syslocation', 'sys location', 'snmp_location')


def load_node_list(path):
    """Read a SolarWinds-style node export (CSV). Column names are matched loosely.
    Returns ({norm_host: {'ip', 'site', 'location'}}, info)."""
    text = read_text(path)
    reader = csv.DictReader(text.splitlines())
    fields = {f.strip().lower(): f for f in (reader.fieldnames or [])}

    def pick(options):
        for o in options:
            if o in fields:
                return fields[o]
        return None

    cols = {'host': pick(_HOST_COLS), 'ip': pick(_IP_COLS), 'site': pick(_SITE_COLS),
            'location': pick(_LOCATION_COLS)}
    nodes = {}
    for row in reader:
        host = (row.get(cols['host']) or '').strip() if cols['host'] else ''
        if not host:
            continue
        nodes[device_key(*split_caption(host))] = {k: (row.get(c) or '').strip() if c else ''
                                                   for k, c in cols.items() if k != 'host'}
    info = dict(('%s_column' % k, c) for k, c in cols.items())
    info['rows'] = len(nodes)
    return nodes, info


# --------------------------------------------------------------------------- devices

def new_device(key, source='SolarWinds'):
    """source: 'SolarWinds' (parsed show output) or 'manual' (the manual inventory sheet).
    stale / last_seen are set when a device SolarWinds could not reach is carried forward."""
    return {'key': key, 'hostname': None, 'note': '', 'source': source, 'stale': False, 'last_seen': '',
            'mgmt_ip': '', 'site': '', 'location': '',
            'platform': '', 'model': '', 'serial': '', 'version': '', 'install_mode': '',
            'uptime': '', 'reload_reason': '', 'router_mode': '', 'image': '', 'members': [],
            'hardware': [], 'no_serial': [], 'optics': 0, 'fex_status': {},
            'vdc_list': [], 'vdc_of': '', 'vdcs': [],
            'commands': set(), 'host_sources': set(), 'files': set(), 'warnings': []}


# Output that is not parsed: every copy is kept (only the command names are recorded).
_UNPARSED = ('unknown', 'ignored', 'neighbors')


def _usable(sec):
    """Output that can be parsed: not a collection error, not empty, not rejected."""
    return (sec.cmd != 'error' and bool(sec.text.strip())
            and not (sec.cmd != 'version' and parsers.is_unsupported(sec.text)))


def _pick_sections(secs):
    """One section per command for a device. Several collections may cover the same device
    (e.g. two SolarWinds jobs that overlap): use the usable output from the newest file, and drop
    a 'could not collect' error when another file did reach the device."""
    mtimes = {}

    def mtime(path):
        if path not in mtimes:
            try:
                mtimes[path] = os.path.getmtime(path)
            except OSError:
                mtimes[path] = 0
        return mtimes[path]
    by_cmd, keep = {}, []
    for i, s in enumerate(secs):
        if s.cmd in _UNPARSED:
            keep.append((i, s))
        else:
            by_cmd.setdefault(s.cmd, []).append((i, s))
    reached = any(s.cmd in ('version', 'inventory') and _usable(s) for s in secs)
    for cmd, cands in by_cmd.items():
        if cmd == 'error' and reached:
            continue
        keep.append(max(cands, key=lambda c: (_usable(c[1]), mtime(c[1].path), c[0])))
    return [s for _, s in sorted(keep, key=lambda c: c[0])]


def _merge_same_device(groups):
    """Join groups that are one box under two names - e.g. 'as1.example.com' in one collection and
    a file named by IP in another: same show version hostname and the same serial (or one has
    none). Different serials or different SolarWinds notes stay apart (same name, two devices)."""
    info = {}
    for key, secs in groups.items():
        vers = [s for s in secs if s.cmd == 'version' and _usable(s)]
        if not vers:
            continue
        v = parsers.parse_version(vers[-1].text)
        if v.get('hostname'):
            info[key] = (norm_host(v['hostname']), real_serial(v.get('serial')),
                         next((s.note for s in secs if s.note), ''))
    owner = dict((k, k) for k in groups)

    def find(k):
        while owner[k] != k:
            k = owner[k]
        return k
    keys = [k for k in groups if k in info]
    for a_i, a in enumerate(keys):
        for b in keys[a_i + 1:]:
            (ha, sa, na), (hb, sb, nb) = info[a], info[b]
            # same serial = same box, whatever the notes; without both serials, notes must agree
            if ha == hb and ((sa and sa == sb) or
                             ((not sa or not sb) and (not na or not nb or na == nb) and (sa or sb or na == nb))):
                ra, rb = find(a), find(b)
                if ra != rb:
                    best = min((ra, rb), key=lambda k: (not ' | ' in k, is_ipv4(k) or k.startswith('unidentified:'),
                                                        list(groups).index(k)))
                    owner[ra] = owner[rb] = best
    # A record with no output of its own (e.g. 'could not collect' in one instance) joins the one
    # collected device of the same hostname - if there is exactly one, so the match is certain.
    by_host = {}
    for k, (h, _, _) in info.items():
        by_host.setdefault(h, set()).add(find(k))
    for k, secs in groups.items():
        if k in info or k.startswith('unidentified:'):
            continue
        host, _, note = k.partition(' | ')
        cands = [c for c in by_host.get(host, ()) if not note or info.get(c, ('', '', ''))[2].lower() in ('', note)]
        if len(cands) == 1:
            owner[k] = cands[0]
    merged = {}
    for k in groups:
        merged.setdefault(find(k), []).extend(groups[k])
    return merged


def build_devices(sections, nodes=None):
    """Group sections by device and parse them. Returns (devices, stats).
    A device covered by several files (overlapping collections) gets one copy of each command."""
    nodes = nodes or {}
    stats = {'sections': {}, 'unsupported': {}, 'inventory_name_lines': 0, 'inventory_items': 0,
             'kinds': {}, 'unrecognised': {}, 'version_missing_lines': [], 'module_unparsed': 0,
             'unidentified_sections': 0, 'vdc_groups': 0, 'no_serial': {}, 'overlapping_devices': 0}
    groups = {}
    for sec in sections:
        stats['sections'][sec.cmd] = stats['sections'].get(sec.cmd, 0) + 1
        if not sec.host:
            stats['unidentified_sections'] += 1
            key = 'unidentified:' + sec.path
        else:
            key = device_key(sec.host, sec.note)
        groups.setdefault(key, []).append(sec)
    groups = _merge_same_device(groups)
    devices = {}
    for key, secs in groups.items():
        dev = devices[key] = new_device(key)
        for sec in secs:
            dev['note'] = dev['note'] or sec.note
            dev['files'].add(sec.path)
            dev['host_sources'].add(sec.host_source or 'none')
            if not dev['hostname'] and sec.host:
                dev['hostname'] = short_host(sec.host)
            if sec.ip_hint and not dev['mgmt_ip']:
                dev['mgmt_ip'] = sec.ip_hint
        if len(dev['files']) > 1:
            stats['overlapping_devices'] += 1
        for sec in _pick_sections(secs):
            _apply_section(dev, sec, stats)
    _finish_all(devices, nodes, stats)
    return sorted(devices.values(), key=lambda d: (d['site'], d['hostname'] or d['key'])), stats


def _apply_section(dev, sec, stats):
    if sec.cmd == 'error':
        dev['warnings'].append('SolarWinds could not collect: ' + sec.text)
        dev['commands'].add('collection error')
        return
    if parsers.is_unsupported(sec.text) and sec.cmd != 'version':
        stats['unsupported'][sec.cmd] = stats['unsupported'].get(sec.cmd, 0) + 1
        dev['commands'].add(sec.cmd + ' (not supported)')
        dev.setdefault('_rejected', {})[sec.cmd] = parsers.unsupported_reason(sec.text)
        return
    dev['commands'].add(sec.cmd)
    if sec.cmd == 'version':
        _apply_version(dev, sec, stats)
    elif sec.cmd == 'inventory':
        _apply_inventory(dev, sec.text, stats)
    elif sec.cmd == 'module':
        _apply_module(dev, sec.text, stats)
    elif sec.cmd == 'location':
        dev['location'] = parsers.parse_location(sec.text) or dev['location']
    elif sec.cmd == 'fex':
        dev['fex_status'].update(parsers.parse_fex(sec.text))
    elif sec.cmd == 'vdc':
        dev['vdc_list'] = parsers.parse_vdc(sec.text)


def _finish_all(devices, nodes, stats):
    for dev in devices.values():
        _finish_device(dev, nodes)
        for it in dev['no_serial']:
            stats['no_serial'][it['kind']] = stats['no_serial'].get(it['kind'], 0) + 1
    stats['vdc_groups'] = _group_vdcs(devices)
    check_duplicate_serials(devices)


def _apply_version(dev, sec, stats):
    v = parsers.parse_version(sec.text)
    for field in ('platform', 'version', 'uptime', 'reload_reason', 'router_mode', 'image',
                  'install_mode'):
        if v.get(field):
            dev[field] = v[field]
    dev['_ver_model'], dev['_ver_serial'] = v.get('model'), v.get('serial')
    dev['_stack'] = v.get('stack') or []
    if v.get('hostname'):
        if sec.host_source == 'prompt' and norm_host(v['hostname']) != norm_host(sec.host):
            dev['warnings'].append('Prompt says %s but show version says %s'
                                   % (sec.host, v['hostname']))
        elif sec.host_source == 'SolarWinds header' and norm_host(v['hostname']) != norm_host(sec.host):
            dev['warnings'].append('SolarWinds node %s but show version says %s'
                                   % (display_name(dev, short_host(sec.host)), v['hostname']))
        dev['hostname'] = short_host(v['hostname'])
    if not v.get('version'):
        dev['warnings'].append('Software version not found in show version')
        stats['version_missing_lines'].append(sec.text.split('\n')[:6])


def _apply_inventory(dev, text, stats):
    items, name_lines = parsers.parse_inventory(text)
    if not items:
        dev['_inv_empty'] = True
    stats['inventory_name_lines'] += name_lines
    stats['inventory_items'] += len(items)
    if name_lines > len(items):
        dev['warnings'].append('%d inventory entries could not be read' % (name_lines - len(items)))
    for it in items:
        it['kind'] = kind_of(it)
        it['slot'] = slot_of(it)
        it['member'] = member_of(it)
        it.update(status='', sw='', role='')
    chassis_pids = {it['pid'] for it in items if it['kind'] == 'chassis' and it['pid']}
    for it in items:
        if it['kind'] in MODULE_KINDS and it['pid'] in chassis_pids:
            it['kind'] = 'builtin'
        stats['kinds'][it['kind']] = stats['kinds'].get(it['kind'], 0) + 1
        if it['kind'] == 'other':
            k = (it['pid'] or '(no PID)', it['descr'][:60])
            stats['unrecognised'][k] = stats['unrecognised'].get(k, 0) + 1
    dev['optics'] += sum(1 for it in items if it['kind'] == 'transceiver')
    dev['hardware'].extend(it for it in items if it['kind'] != 'stack')   # optics: see hide_optics


def _apply_module(dev, text, stats):
    mod = parsers.parse_module(text)
    stats['module_unparsed'] += mod['unparsed_rows']
    dev['_modules'] = mod['rows']


def _finish_device(dev, nodes):
    if not dev['hostname']:
        dev['hostname'] = dev['key']
    node = nodes.get(dev['key']) or nodes.get(norm_host(dev['hostname']))
    if node:
        dev['mgmt_ip'] = node.get('ip') or dev['mgmt_ip']
        for k in ('site', 'location'):
            dev[k] = node.get(k) or dev[k]

    hw = dev['hardware']
    _merge_module_status(dev)

    # Stack members: inventory 'Switch N' entries + show version member table.
    table = {str(m['number']): m for m in dev.pop('_stack', [])}
    for it in hw:
        if it['kind'] == 'stack_member':
            t = table.get(it['slot'], {})
            it['sw'] = it['sw'] or (t.get('sw_version') or '')
            it['role'] = it['role'] or ('active' if t.get('active') else ('member' if t else ''))
    if not any(it['kind'] == 'stack_member' for it in hw) and len(table) > 1:
        for num, t in sorted(table.items()):      # no inventory: fall back to show version table
            hw.append({'kind': 'stack_member', 'slot': num, 'member': '', 'name': 'Switch ' + num,
                       'descr': t.get('model') or '', 'pid': t.get('model') or '', 'vid': '',
                       'serial': real_serial(t.get('serial')), 'status': '',
                       'sw': t.get('sw_version') or '', 'role': 'active' if t.get('active') else 'member'})
    if not any(it['kind'] in ('chassis', 'stack_member') for it in hw) and dev.get('_ver_model'):
        # no chassis in show inventory (or none collected): the box itself, from show version
        hw.insert(0, {'kind': 'chassis', 'slot': '', 'member': '', 'name': 'Chassis (show version)',
                      'descr': '', 'pid': dev['_ver_model'], 'vid': '',
                      'serial': real_serial(dev.get('_ver_serial')), 'status': '', 'sw': '', 'role': ''})

    # FEX: state and name from 'show fex'.
    for it in hw:
        if it['kind'] == 'fex':
            num = it['member'].replace('FEX ', '')
            st = dev['fex_status'].get(num, {})
            it['status'] = st.get('state', '')
            it['role'] = st.get('description', '')
            it['notes'] = parsers.fex_location(st.get('description'), num) or it.get('notes', '')
            if st and st.get('state', '').lower() != 'online':
                dev['warnings'].append('%s state: %s' % (it['member'], st.get('state')))
    in_inventory_fex = set(it['member'] for it in hw if it['kind'] == 'fex')
    for num, st in sorted(dev['fex_status'].items()):
        if 'FEX ' + num in in_inventory_fex:
            continue
        serial = real_serial(st.get('serial'))
        if serial:            # e.g. show inventory cut short: take the FEX from show fex
            hw.append({'kind': 'fex', 'slot': '', 'member': 'FEX ' + num, 'name': 'FEX %s CHASSIS' % num,
                       'descr': st.get('model', ''), 'pid': st.get('model', ''), 'vid': '', 'serial': serial,
                       'status': st.get('state', ''), 'sw': '', 'role': st.get('description', ''),
                       'notes': parsers.fex_location(st.get('description'), num)})
            if 'inventory' in dev['commands']:
                dev['warnings'].append('FEX %s is in show fex but not in show inventory - taken from show fex'
                                       % num)
        if st.get('state', '').lower() != 'online':
            dev['warnings'].append('FEX %s state: %s%s' % (num, st.get('state') or '?',
                                                          '' if serial else ' (not in show inventory)'))

    drop_unserialized(dev)
    versions = {m['sw'] for m in dev['members'] if m['sw']}
    if len(versions) > 1:
        dev['warnings'].append('Stack members run different software: ' + ', '.join(sorted(versions)))
    for it in dev['hardware']:
        if it['kind'] != 'fex' and it['status'] and not OK_STATUS.match(it['status']):
            dev['warnings'].append('%s %s status: %s' % (it['name'], it['pid'], it['status']))
    if 'collection error' not in dev['commands']:
        rejected = dev.pop('_rejected', {})
        for cmd, label in (('version', 'show version'), ('inventory', 'show inventory')):
            if cmd in dev['commands']:
                continue
            if cmd in rejected:
                dev['warnings'].append('%s was rejected by the device: %s' % (label, rejected[cmd]))
            else:
                dev['warnings'].append('No %s output in the results (the command did not run or its '
                                       'output was lost) - collect it again for this device' % label)
        if dev.pop('_inv_empty', False) and 'inventory' in dev['commands']:
            dev['warnings'].append('show inventory output had no entries')
        if not dev['serial'] and not dev['hardware']:
            dev['warnings'].append('No serial number found - left out of the inventory')
    if dev['key'].startswith('unidentified:'):
        dev['warnings'].append('Could not tell which device this output belongs to')
    for k in ('_ver_model', '_ver_serial', '_modules', '_rejected', '_inv_empty'):
        dev.pop(k, None)


def drop_unserialized(dev):
    """Keep only hardware with a real serial number; set the device's model, serial and stack
    members from what is left. Safe to run again (e.g. on a snapshot from an older version)."""
    keep, gone, seen = [], [], set()
    for it in dev['hardware']:
        if it['kind'] == 'builtin':        # part of the chassis (N9K 'Slot 1', a FEX's own module)
            continue
        it['serial'] = real_serial(it.get('serial'))
        if not it['serial']:
            gone.append(it)
        elif it['serial'] not in seen:     # one serial = one part: both ends of a stack / DAC
            seen.add(it['serial'])         # cable, SMART fans sharing a serial
            keep.append(it)
    dev['hardware'] = keep
    dev['members'] = [it for it in keep if it['kind'] == 'stack_member']
    dev['no_serial'] = dev.get('no_serial', []) + [
        {'kind': it['kind'], 'name': it['name'], 'pid': it['pid']} for it in gone]
    shown = [it for it in gone if it['kind'] in _REPORT_UNSERIALIZED]
    if shown:
        dev['warnings'].append('Left out (no serial number): ' + ', '.join(
            '%s %s' % (it['name'], it['pid']) for it in shown))

    chassis, members = [it for it in keep if it['kind'] == 'chassis'], dev['members']
    if chassis:
        dev['model'] = ' + '.join(c['pid'] for c in chassis)
        dev['serial'] = ' / '.join(c['serial'] for c in chassis)
    elif members:
        pids = sorted({m['pid'] for m in members})
        dev['model'] = ', '.join(pids) + (' (stack of %d)' % len(members) if len(members) > 1 else '')
        active = [m for m in members if m['role'] == 'active'] or members
        dev['serial'] = active[0]['serial']
    return gone


def hide_optics(devices):
    """Take optics (SFPs etc.) out of the hardware lists - the default: there are thousands and
    nobody tracks them. They stay in the snapshots, so turning the option on needs no re-import."""
    for d in devices:
        d['hardware'] = [it for it in d['hardware'] if it['kind'] != 'transceiver']
    return devices


def display_name(dev, hostname=None):
    """How a device is shown: its hostname, plus the SolarWinds note when its node has one
    ('campus-as1 (BLDG 2)'), so devices sharing a hostname can be told apart."""
    host = hostname or dev['hostname']
    return '%s (%s)' % (host, dev['note']) if dev.get('note') else host


def _names(dev):
    return {display_name(dev).lower(), (dev['hostname'] or '').lower(), dev['key']}


def excluded_by(dev, patterns):
    """The first exclusion pattern matching the device, or None. Patterns are hostname
    wildcards, any case: 'PS-*', '*-VG?', 'campus-as1 (BLDG 2)'. A pattern starting with '!'
    keeps matching devices whatever the other rules say: '!PS-CORE1'."""
    names = _names(dev)

    def hit(p):
        return any(fnmatch.fnmatchcase(n, p.strip().lower()) for n in names)
    patterns = [p for p in patterns or () if p.strip()]
    if any(hit(p.strip()[1:]) for p in patterns if p.strip().startswith('!')):
        return None
    for p in patterns:
        if not p.strip().startswith('!') and hit(p):
            return p
    return None


def include_rules(dev, patterns):
    """The rules after bringing one excluded device back: its own exact-name rule is removed;
    if a wildcard caught it ('PS-*'), a keep rule '!<name>' is added instead, so the rest of
    what the wildcard matches stays excluded."""
    rule = excluded_by(dev, patterns)
    if rule is None:
        return list(patterns)
    if not any(c in rule for c in '*?[') and rule.strip().lower() in _names(dev):
        return [p for p in patterns if p != rule]
    return list(patterns) + ['!' + display_name(dev)]


def in_inventory(dev):
    """Devices with a serial number (or serialized parts, e.g. a bench shelf) are inventory;
    the rest only appear on the Issues list."""
    return bool(dev['serial'] or dev['hardware'])


def _group_vdcs(devices):
    """Nexus 7000/7700 VDCs (e.g. a campus VDC and an admin VDC) are separate SolarWinds
    nodes on one chassis: same chassis serial. Keep the hardware on one 'primary' record
    (the VDC named in 'show vdc' as the Ethernet one, else the first by name) so it is
    counted once. Returns the number of groups."""
    by_serial = {}
    for d in devices.values():
        sn = d['serial']
        if sn and d['platform'] == 'NX-OS' and any(h['kind'] == 'chassis' for h in d['hardware']):
            by_serial.setdefault(sn, []).append(d)
    groups = 0
    for sn, group in by_serial.items():
        if len(group) < 2:
            continue
        groups += 1
        admin = {name.lower() for d in group for _, name, typ in d['vdc_list'] if typ.lower() == 'admin'}
        group.sort(key=lambda d: (d['hostname'].lower() in admin, d['hostname']))
        primary = group[0]
        primary['vdcs'] = sorted(d['hostname'] for d in group)
        for d in group[1:]:
            d['vdc_of'] = primary['hostname']
            d['hardware'] = []
            d['members'] = []
    return groups


def _merge_module_status(dev):
    rows = dev.get('_modules') or {}
    if not rows:
        return
    by_serial = {r['serial']: r for r in rows.values() if r.get('serial')}
    for it in dev['hardware']:
        row = by_serial.get(it['serial']) if it['serial'] else None
        if row is None and it['kind'] in MODULE_KINDS + ('stack_member',) and it['slot']:
            row = rows.get(it['slot'])
        if row:
            it['status'] = row.get('status', '') or it['status']
            it['sw'] = row.get('sw', '') or it['sw']
            it['role'] = (row.get('role', '') or it['role']).lower()


def check_duplicate_serials(devices):
    """Same serial on two devices is suspicious - except dual-homed FEX (one FEX, two parents)."""
    seen = {}
    for dev in devices.values():
        for it in dev['hardware']:
            if it['kind'] not in ('builtin', 'fex') and it['member'][:3] != 'FEX':
                seen.setdefault(it['serial'], set()).add(dev['hostname'])
    for sn, hosts in seen.items():
        if len(hosts) > 1:
            for dev in devices.values():
                if dev['hostname'] in hosts:
                    dev['warnings'].append('Serial %s also appears on %s'
                                           % (sn, ', '.join(sorted(hosts - {dev['hostname']}))))


# --------------------------------------------------------------------------- part counts

def part_counts(networks):
    """Unique part numbers and how many of each there are, per network and in total.

    networks: [(name, devices)]. Each physical part is counted once, by serial number, so a
    dual-homed FEX (listed under both parents) or a chassis shared by VDCs counts as one.
    Built-in parts (the chassis' own ports / route processor) are not counted separately.
    Returns rows sorted by kind then PID:
        [{'pid', 'kind', 'descr', 'counts': {network: n}, 'total': n}]"""
    rows = {}

    def parts(sets):
        for name, devices in sets:
            for d in devices:
                for it in d['hardware']:
                    if it['kind'] != 'builtin' and it['serial']:
                        pid = it['pid'] or '(no PID)'
                        r = rows.setdefault(pid, {'pid': pid, 'kind': it['kind'], 'descr': it['descr'],
                                                  'serials': {}, 'all': set()})
                        r['descr'] = r['descr'] or it['descr']
                        yield name, it, r
    for name, it, r in parts(networks):
        r['serials'].setdefault(name, set()).add(it['serial'])
        r['all'].add(it['serial'])
    out = []
    for r in rows.values():
        out.append({'pid': r['pid'], 'kind': r['kind'], 'descr': r['descr'],
                    'counts': {n: len(s) for n, s in r['serials'].items()}, 'total': len(r['all'])})
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    out.sort(key=lambda r: (order.get(r['kind'], len(order)), r['pid']))
    return out
