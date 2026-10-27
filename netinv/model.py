"""Merge parsed sections into device records and hardware lists, and count parts.

Only hardware with a real serial number is kept: a provisioned-but-absent stack member, an
empty slot or a fan without a serial EEPROM is left out (and listed on the device's issues
when it is a chassis, stack member, FEX or module)."""

import csv
import fnmatch
import os
import re

from . import parsers
from .kinds import kind_of, slot_of, member_of, MODULE_KINDS, KIND_ORDER, extra_kind
from .textutil import device_key, is_ip, is_ipv4, norm_host, read_text, real_serial, short_host, split_caption

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
_HW_IDENTITY = ('chassis', 'stack_member')     # parts whose serials identify the box itself


class _Parsed(object):
    """What one section yields, worked out once: quality and the serials it names - 'inv' from
    show inventory (chassis / stack members), 'ver' from show version (on NX-OS that is the
    supervisor's, so the two kinds are only compared with their own kind)."""
    cache = {}

    @classmethod
    def of(cls, sec):
        key = id(sec)
        hit = cls.cache.get(key)
        if hit is None or hit[0] is not sec:
            hit = cls.cache[key] = (sec, cls._work(sec))
        return hit[1]

    @staticmethod
    def _work(sec):
        out = {'quality': 0, 'hostname': '', 'inv': set(), 'ver': set(), 'all': set()}
        if sec.cmd == 'error' or not sec.text.strip() or parsers.is_unsupported(sec.text):
            return out                          # nothing usable: an error, empty or rejected
        out['quality'] = 1
        if sec.cmd == 'version':
            v = parsers.parse_version(sec.text)
            out['hostname'] = norm_host(v.get('hostname') or '')
            sn = real_serial(v.get('serial'))
            if sn:
                out['ver'].add(sn)
            if out['hostname'] or sn:
                out['quality'] = 2              # a real show version (not e.g. a neighbour's banner)
        elif sec.cmd == 'inventory':
            items, _ = parsers.parse_inventory(sec.text)
            if items:
                out['quality'] = 2
            for it in items:
                if not it['serial']:
                    continue
                out['all'].add(it['serial'])
                kind = kind_of(it)
                # the box's own line: a recognised chassis / member, or an unrecognised line whose
                # description says so ('CISCO2911/K9 chassis, Hw Serial#...', see _find_chassis)
                if kind in _HW_IDENTITY or (kind == 'other' and re.search(r'(?i)\bchassis\b', it['descr'] or '')):
                    out['inv'].add(it['serial'])
        else:
            out['quality'] = 2
        return out


def _usable(sec):
    """Output that can be parsed: not a collection error, not empty, not rejected."""
    return _Parsed.of(sec)['quality'] > 0


def _pick_sections(secs):
    """One section per command for a device. Several collections may cover the same device
    (e.g. two SolarWinds instances that overlap). Best first: output that parses (a show version
    with a hostname or serial, an inventory with entries), then output that followed the command's
    own echo (not just recognised by its content), then the newest file. A 'could not collect'
    error is dropped when another collection did reach the device."""
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
        keep.append(max(cands, key=lambda c: (_Parsed.of(c[1])['quality'],
                                              not (c[1].how or '').endswith('+content'),
                                              mtime(c[1].path), c[0])))
    return [s for _, s in sorted(keep, key=lambda c: c[0])]


def _different_boxes(a, b):
    """Do the serials prove two different boxes? Chassis / stack serials from show inventory are
    compared with each other; show version serials only when an inventory is missing on a side,
    and a show version serial that appears in the other side's inventory is the same box."""
    if a['inv'] and b['inv']:
        return not (a['inv'] & b['inv'])
    if a['ver'] and b['ver']:
        return not ((a['ver'] | a['inv']) & (b['ver'] | b['inv']))
    if (a['inv'] or a['ver']) and (b['inv'] or b['ver']) and a.get('ips') and b.get('ips'):
        # one side has only inventory serials, the other only a show version serial: they
        # cannot be compared. Different IP addresses then mean two boxes (listed twice at
        # worst - better than one of them vanishing into the other)
        # A show version serial anywhere in the other side's inventory is the same box (NX-OS
        # reports the supervisor's serial; an ISR G2 its chassis line's)
        if (a['ver'] & b.get('all', set())) or (b['ver'] & a.get('all', set())):
            return False
        return not ((a['inv'] | a['ver']) & (b['inv'] | b['ver'])) and not (a['ips'] & b['ips'])
    return False


def _caption(sec):
    """How SolarWinds (or the file) names the node: 'as1', 'as1 (BLDG 2)', '10.1.1.1'."""
    host = short_host(sec.host or '')
    return '%s (%s)' % (host, sec.note) if sec.note else host


def _group_sections(sections, stats):
    """Sections -> {device key: [sections]}: one device per physical box.

    Each SolarWinds block (one node in one results file) is a unit. Blocks join when the evidence
    says they are the same box, never on a name alone:
      * same caption (device key) - unless their serial numbers show two different boxes
        (one instance listing another box under that caption);
      * different captions, same show version hostname and an overlapping chassis / stack
        serial, or the same SolarWinds IP address (the same box named differently in two
        SolarWinds instances, e.g. 'as1' / 'as1 (BLDG 2)' / '10.1.1.1');
      * a block with no usable output (e.g. 'could not collect') joins the one device with the
        same IP address.
    Returns ({key: [sections]}, {key: note}): every join across captions gets a note for the
    device's issues, so nothing is merged silently."""
    blocks, order = {}, []
    for sec in sections:
        stats['sections'][sec.cmd] = stats['sections'].get(sec.cmd, 0) + 1
        if not sec.host:
            stats['unidentified_sections'] += 1
            key = 'unidentified:' + sec.path
        else:
            key = device_key(sec.host, sec.note)
        bid = (key, getattr(sec, 'block', None) or (sec.path,))
        if bid not in blocks:
            blocks[bid] = {'key': key, 'secs': [], 'ips': set(), 'inv': set(), 'ver': set(), 'all': set(), 'host': '',
                           'data': False, 'caption': _caption(sec) if sec.host else key}
            order.append(bid)
        b = blocks[bid]
        b['secs'].append(sec)
        if sec.ip_hint:
            b['ips'].add(sec.ip_hint)
        p = _Parsed.of(sec)
        b['inv'] |= p['inv']
        b['ver'] |= p['ver']
        b['all'] |= p['all']
        if sec.cmd in ('version', 'inventory') and p['quality'] > 0:
            b['data'] = True
        if p['hostname'] and (not b['host'] or p['quality'] == 2):
            b['host'] = p['hostname']

    owner = dict((bid, bid) for bid in order)
    comp = dict((bid, {'inv': set(blocks[bid]['inv']), 'ver': set(blocks[bid]['ver']), 'ips': set(blocks[bid]['ips']),
                       'all': set(blocks[bid]['all']),
                       'hosts': set([blocks[bid]['host']]) - set([''])}) for bid in order)
    why = {}

    def find(x):
        while owner[x] != x:
            owner[x] = owner[owner[x]]
            x = owner[x]
        return x

    def join(a, b, reason=None):
        ra, rb = find(a), find(b)
        if ra == rb:
            return True
        ca, cb = comp[ra], comp[rb]
        if _different_boxes(ca, cb):
            return False                        # two different boxes: never one device
        owner[rb] = ra
        ca['inv'] |= cb['inv']
        ca['ver'] |= cb['ver']
        ca['all'] |= cb['all']
        ca['ips'] |= cb['ips']
        ca['hosts'] |= cb['hosts']
        if reason:
            why.setdefault(ra, set()).add(reason)
        why.setdefault(ra, set()).update(why.pop(rb, set()))
        return True

    # 1. The same caption: one device, unless the serials say otherwise.
    by_key = {}
    for bid in order:
        by_key.setdefault(blocks[bid]['key'], []).append(bid)
    for key, bids in by_key.items():
        with_ev = [b for b in bids if blocks[b]['inv'] or blocks[b]['ver']]
        without = [b for b in bids if not (blocks[b]['inv'] or blocks[b]['ver'])]
        for b in with_ev[1:]:
            for a in with_ev:
                if a != b and join(a, b):
                    break
        for b in without:                       # no serials: join the caption's device with the
            ips = blocks[b]['ips']              # same IP (or, without IPs, its first one)
            targets = [a for a in with_ev if not ips or not blocks[a]['ips'] or blocks[a]['ips'] & ips]
            if targets:
                join(targets[0], b)
            else:
                others = [a for a in without if a != b and (not ips or not blocks[a]['ips']
                                                             or blocks[a]['ips'] & ips)]
                if others and others[0] != b:
                    join(others[0], b)
    # 2. Different captions, same box: same show version hostname plus a serial or IP in common.
    roots = sorted(set(find(b) for b in order), key=order.index)
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            ca, cb = comp[ra], comp[rb]
            if not (ca['hosts'] & cb['hosts']):
                continue
            if (ca['inv'] | ca['ver']) & (cb['inv'] | cb['ver']):
                join(ra, rb, 'same serial number')
            elif ca['ips'] & cb['ips']:
                join(ra, rb, 'same IP address and hostname')
    # 3. A node with no usable output anywhere joins the one device with its IP address.
    for bid in order:
        r = find(bid)
        if any(blocks[x]['data'] for x in order if find(x) == r):
            continue
        cands = set(find(x) for x in order if find(x) != r and blocks[x]['data']
                    and blocks[x]['ips'] & comp[r]['ips'])
        if len(cands) == 1:
            join(cands.pop(), r, 'same IP address; not reached under that name')

    groups, captions = {}, {}
    for bid in order:
        r = find(bid)
        groups.setdefault(r, []).extend(blocks[bid]['secs'])
        captions.setdefault(r, [])
        if blocks[bid]['caption'] not in captions[r]:
            captions[r].append(blocks[bid]['caption'])
    out, notes, used = {}, {}, set()
    for r, secs in groups.items():
        keys = []
        for x in order:
            if find(x) == r and blocks[x]['key'] not in keys:
                keys.append(blocks[x]['key'])
        key = min(keys, key=lambda k: (' | ' not in k, is_ip(k) or k.startswith('unidentified:'),
                                       keys.index(k)))
        n = 2
        base = key
        while key in used:                      # one caption, two boxes: keep both
            key = '%s #%d' % (base, n)
            n += 1
        used.add(key)
        out[key] = secs
        others = [c for c in captions[r] if device_key(*split_caption(c)) != base]
        if others and why.get(r):
            stats['combined'] = stats.get('combined', 0) + len(others)
            notes[key] = ('Also listed in SolarWinds as %s - counted once (%s)'
                          % (', '.join(others), ', '.join(sorted(why[r]))))
    return out, notes


def build_devices(sections, nodes=None):
    """Group sections by device and parse them. Returns (devices, stats).
    A device covered by several files (overlapping collections) gets one copy of each command."""
    nodes = nodes or {}
    stats = {'sections': {}, 'unsupported': {}, 'inventory_name_lines': 0, 'inventory_items': 0,
             'kinds': {}, 'unrecognised': {}, 'version_missing_lines': [], 'module_unparsed': 0,
             'unidentified_sections': 0, 'vdc_groups': 0, 'no_serial': {}, 'overlapping_devices': 0,
             'combined': 0}
    groups, notes = _group_sections(sections, stats)
    devices = {}
    for key, secs in groups.items():
        dev = devices[key] = new_device(key)
        if key in notes:
            dev['warnings'].append(notes[key])
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
    _Parsed.cache.clear()
    return sorted(devices.values(), key=lambda d: (d['site'], d['hostname'] or d['key'])), stats


def _apply_section(dev, sec, stats):
    if sec.cmd == 'error':
        dev['warnings'].append('SolarWinds could not collect: ' + sec.text)
        dev['commands'].add('collection error')
        return
    if parsers.is_unsupported(sec.text):
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


def _find_chassis(dev, hw, stacked):
    """No inventory entry was recognised as the chassis (a model whose chassis line is not
    named 'Chassis' / 'Switch N', e.g. 'VG310' or 'CISCO2911/K9'). Pick the unrecognised entry
    that says it is one, best first:
      1. its description says 'chassis' (an ISR G2 / VG3x0 'CISCO2911/K9 chassis, Hw Serial#')
      2. it carries show version's serial (Processor board ID)
      3. its PID is show version's model
      4. no show version at all: the one entry named after its own PID - only if there is
         exactly one (an IE3400 lists its expansion module the same way: then nothing)
    Nothing else - a route processor, a disk or a module listed first is not the box."""
    if stacked:
        return
    ver_sn, ver_model = real_serial(dev.get('_ver_serial')), dev.get('_ver_model') or ''
    cands = [it for it in hw if it['kind'] in ('other', 'builtin') and real_serial(it.get('serial'))
             and not member_of(it)]
    tests = [lambda it: re.search(r'(?i)\bchassis\b', it['descr'] or '') and (not ver_model or it['pid'] == ver_model),
             lambda it: ver_sn and real_serial(it['serial']) == ver_sn,
             lambda it: ver_model and it['pid'] == ver_model]
    named = [it for it in cands if it['pid'] and it['name'].strip().upper() == it['pid'].strip().upper()]
    if not ver_sn and not ver_model and len(named) == 1:
        tests.append(lambda it: it is named[0])
    for test in tests:
        hit = [it for it in cands if test(it)]
        if hit:
            hit[0]['kind'] = 'chassis'
            return


_OWN_LINE = re.compile(r'(?i)\b(system|chassis)\b')


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
    if not any(it['kind'] in ('chassis', 'stack_member') for it in hw):
        _find_chassis(dev, hw, len(table) > 1)
    if not any(it['kind'] in ('chassis', 'stack_member') for it in hw) and dev.get('_ver_model'):
        # no chassis in show inventory (or none collected): the box itself, from show version
        hw.insert(0, {'kind': 'chassis', 'slot': '', 'member': '', 'name': 'Chassis (show version)',
                      'descr': '', 'pid': dev['_ver_model'], 'vid': '',
                      'serial': real_serial(dev.get('_ver_serial')), 'status': '', 'sw': '', 'role': ''})
    chassis_pids = {it['pid'] for it in hw if it['kind'] == 'chassis' and it['pid']}
    for it in hw:
        # unrecognised, but the chassis' own PID: part of it (a VG310's FXS ports, a board).
        # Not a line that is a box of its own: 'Switch 2 System' of a VSS pair, 'Chassis 2'
        if (it['kind'] == 'other' and it['pid'] in chassis_pids and not member_of(it)
                and not _OWN_LINE.search(it['name'] or '')):
            it['kind'] = 'builtin'

    # FEX: state and name from 'show fex'.
    for it in hw:
        if it['kind'] == 'fex':
            num = it['member'].replace('FEX ', '')
            st = dev['fex_status'].get(num, {})
            it['status'] = st.get('state', '')
            it['role'] = ''
            it['location'] = st.get('description', '')     # the 'show fex' description: where it is
            if st.get('state') and st['state'].lower() != 'online':
                dev['warnings'].append('%s state: %s' % (it['member'], st['state']))
    in_inventory_fex = set(it['member'] for it in hw if it['kind'] == 'fex')
    for num, st in sorted(dev['fex_status'].items()):
        if 'FEX ' + num in in_inventory_fex:
            continue
        serial = real_serial(st.get('serial'))
        if serial:            # e.g. show inventory cut short: take the FEX from show fex
            hw.append({'kind': 'fex', 'slot': '', 'member': 'FEX ' + num, 'name': 'FEX %s CHASSIS' % num,
                       'descr': st.get('model', ''), 'pid': st.get('model', ''), 'vid': '', 'serial': serial,
                       'status': st.get('state', ''), 'sw': '', 'role': '',
                       'location': st.get('description', '')})
            if 'inventory' in dev['commands']:
                dev['warnings'].append('FEX %s is in show fex but not in show inventory - taken from show fex'
                                       % num)
        if st.get('state') and st['state'].lower() != 'online':
            dev['warnings'].append('FEX %s state: %s%s' % (num, st['state'],
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
    keep, gone, seen, builtin = [], [], set(), []
    for it in dev['hardware']:
        if it['kind'] == 'builtin':
            continue
        it['serial'] = real_serial(it.get('serial'))
        if not it['serial']:
            gone.append(it)
        elif it['serial'] not in seen:     # one serial = one part: both ends of a stack / DAC
            seen.add(it['serial'])         # cable, SMART fans sharing a serial
            keep.append(it)
    for it in dev['hardware']:
        # part of the chassis (a VG310's FXS ports, a router's 'module R0', a FEX's own module):
        # listed, never counted; dropped when it repeats a serial already listed (N9K 'Slot 1'
        # carries the chassis serial), quietly when it has none
        if it['kind'] == 'builtin':
            it['serial'] = real_serial(it.get('serial'))
            if it['serial']:
                builtin.append(it)
                if it['serial'] not in seen:
                    seen.add(it['serial'])
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
    elif not dev.get('serial') and builtin:
        # no chassis serial anywhere else (e.g. a router whose 'Chassis' line has none): the
        # serial of a part built into the chassis identifies the box
        dev['model'] = dev.get('model') or builtin[0]['pid']
        dev['serial'] = real_serial(builtin[0]['serial'])
    return gone


def apply_kind_rules(devices):
    """The local rules (kinds.set_extra_rules) on a loaded snapshot: they name parts, never a
    chassis / stack member / FEX. Then flag what is still unrecognised, one issue per device."""
    for d in devices:
        left = []
        boxes = set(it['pid'] for it in d['hardware'] if it['kind'] in ('chassis', 'stack_member') and it['pid'])
        for it in d['hardware']:
            # not: a Kind typed on the manual sheet, or a part with the box's own PID (a
            # chassis' built-in ports: counting them would count the box twice)
            if not it.get('kind_given') and it['pid'] not in boxes:
                it['kind'] = extra_kind(it) or it['kind']
            if it['kind'] == 'other':
                left.append(it)
        if left and d.get('source') != 'manual':
            d['warnings'].append('Unrecognised part%s (listed and counted as Unrecognised; '
                                 'double-click it on the Inventory tab to name it): %s'
                                 % ('s' if len(left) > 1 else '',
                                    ', '.join('%s %s' % (it['name'], it['pid']) for it in left)))


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


_BOX_KINDS = ('chassis', 'stack_member', 'fex')


def part_identity(dev, it):
    """What makes a part one physical part, for counting. A chassis, stack member or FEX is its
    serial (a dual-homed FEX under two parents, a chassis shared by VDCs: one part). Any other
    part is its serial within its box: two modules / PSUs in different boxes that report the
    same serial (a default or placeholder serial) are two parts. Parts inside a FEX belong to
    the FEX, whichever parent lists them."""
    if it['kind'] in _BOX_KINDS or it.get('member', '')[:3] == 'FEX':
        return it['serial']
    return (it['serial'], dev.get('serial') or dev['key'])


def mark_shared_serials(devices, serials):
    """Apply the acknowledged shared / default serials (settings 'shared_serials'): a module, PSU
    or fan reporting one is marked shared_serial (still counted once per box, see part_identity)
    and the 'default serial shared by two parts' warnings for it are dropped. A chassis or stack member
    with such a serial is not marked - that warning stays, it is a different problem."""
    serials = set(serials or ())
    if not serials:
        return
    for d in devices:
        for it in d['hardware']:
            if it['serial'] in serials and it['kind'] not in _BOX_KINDS:
                it['shared_serial'] = True
        d['warnings'] = [w for w in d['warnings'] if (SHARED_WARNING.match(w) or [0, None])[1] not in serials]


SHARED_WARNING = re.compile(r'Serial (\S+) on .* default serial shared by two parts')


def check_duplicate_serials(devices):
    """The same serial on two devices: name the part on each side. On a chassis or stack member
    that is suspicious; on a module, PSU or fan it is usually a default serial shared by two
    different parts, which are counted separately. A dual-homed FEX is expected and not listed."""
    seen = {}
    for dev in devices.values():
        for it in dev['hardware']:
            if it['kind'] not in ('builtin', 'fex') and it['member'][:3] != 'FEX':
                seen.setdefault(it['serial'], []).append((dev, it))
    for sn, uses in seen.items():
        hosts = set(display_name(d) for d, _ in uses)
        if len(hosts) < 2:
            continue
        for dev, it in uses:
            others = ', '.join(sorted(hosts - {display_name(dev)}))
            if it['kind'] in _BOX_KINDS:
                dev['warnings'].append('Serial %s (%s %s) also appears on %s' % (sn, it['name'], it['pid'], others))
            else:
                dev['warnings'].append('Serial %s on %s %s is also reported by a part in %s - probably a '
                                       'default serial shared by two parts; counted separately'
                                       % (sn, it['name'], it['pid'], others))


# --------------------------------------------------------------------------- part counts

def part_counts(networks):
    """Unique part numbers and how many of each there are, per network and in total.

    networks: [(name, devices)]. Each physical part is counted once (part_identity): a
    dual-homed FEX (listed under both parents) or a chassis shared by VDCs counts as one, while
    two modules in different boxes that report the same default serial count as two.
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
                        yield name, d, it, r
    for name, d, it, r in parts(networks):
        ident = part_identity(d, it)
        r['serials'].setdefault(name, set()).add(ident)
        r['all'].add(ident)
    out = []
    for r in rows.values():
        out.append({'pid': r['pid'], 'kind': r['kind'], 'descr': r['descr'],
                    'counts': {n: len(s) for n, s in r['serials'].items()}, 'total': len(r['all'])})
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    out.sort(key=lambda r: (order.get(r['kind'], len(order)), r['pid']))
    return out
