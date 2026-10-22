"""Parsers for Cisco IOS / IOS-XE / NX-OS show-command output.

Every parser is tolerant: it extracts what it recognises and reports what it
could not, rather than failing. Formats follow Cisco's published output for
Catalyst 9000, Catalyst 8000, ISR/ASR, Nexus 7000/9000 and classic IOS switches.
"""

import re

from .textutil import real_serial

# How IOS / IOS-XE / NX-OS say "I don't do that command" - such sections are skipped.
INVALID_RE = re.compile(r'^\s*(% ?(Invalid input|Invalid command|Incomplete command|Ambiguous command|'
                        r'.*not enabled|Feature not enabled|.*is not running)|'
                        r'Syntax error while parsing)', re.M | re.I)


def is_unsupported(text):
    return bool(INVALID_RE.search(text))


def unsupported_reason(text):
    """The device's own error line ('% Invalid input detected at ...'), for the Issues list."""
    m = INVALID_RE.search(text or '')
    return text[m.start():].split('\n')[0].strip() if m else ''


def _first(rx, text, flags=re.M):
    m = re.search(rx, text, flags)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- show version

def parse_version(text):
    r = {'platform': None, 'version': None, 'hostname': None, 'uptime': None, 'model': None,
         'serial': None, 'image': None, 'reload_reason': None, 'router_mode': None,
         'confreg': None, 'install_mode': None, 'stack': [], 'unsupported': is_unsupported(text)}

    if re.search(r'Cisco Nexus Operating System|^\s*NXOS:\s+version', text, re.M):
        r['platform'] = 'NX-OS'
    elif re.search(r'IOS[ -]XE Software|IOSXE|X86_64_LINUX_IOSD', text):
        r['platform'] = 'IOS-XE'
    elif re.search(r'Cisco IOS Software|Internetwork Operating System', text):
        r['platform'] = 'IOS'

    if r['platform'] == 'NX-OS':
        r['version'] = _first(r'^\s*(?:NXOS|NX-OS|system):\s+version\s+(\S+)', text, re.M | re.I)
        r['hostname'] = _first(r'^\s*Device name:\s*(\S+)', text)
        r['uptime'] = _first(r'^\s*Kernel uptime is\s+(.+?)\s*$', text)
        r['image'] = _first(r'^\s*(?:NXOS|system) image file is:\s*(\S+)', text, re.M | re.I)
        r['model'] = _first(r'^\s*cisco\s+Nexus\s*\d*\s+(\S+)\s.*[Cc]hassis', text)
        r['serial'] = _first(r'Processor [Bb]oard ID\s+(\S+)', text)
        m = re.search(r'^\s*Last reset.*\n(?:.*\n){0,2}?\s*Reason:\s*(.+?)\s*$', text, re.M)
        r['reload_reason'] = m.group(1) if m else None
        return r

    r['version'] = (_first(r'Cisco IOS[ -]XE Software,?\s+Version\s+(\S+)', text)
                    or _first(r'Cisco IOS Software.*?,\s*Version\s+([^,\s]+)', text))
    m = re.search(r'^(?!Kernel |System |Uptime |Switch )(\S+)\s+uptime is\s+(.+?)\s*$', text, re.M)
    if m:
        r['hostname'], r['uptime'] = m.group(1), m.group(2)
    r['image'] = _first(r'System image file is\s+"([^"]+)"', text)
    r['reload_reason'] = _first(r'^\s*Last reload reason:\s*(.+?)\s*$', text)
    r['router_mode'] = _first(r'^\s*Router operating mode:\s*(.+?)\s*$', text)
    r['confreg'] = _first(r'^\s*Configuration register is\s+(\S+)', text)
    r['model'] = (_first(r'^\s*Model [Nn]umber\s*:\s*(\S+)', text)
                  or _first(r'^\s*cisco\s+(\S+)(?:\s+\([^)]*\))?\s+processor', text, re.M | re.I))
    r['serial'] = (_first(r'^\s*System [Ss]erial [Nn]umber\s*:\s*(\S+)', text)
                   or _first(r'Processor [Bb]oard ID\s+(\S+)', text))

    r['stack'] = _parse_stack_table(text, r)
    if not r['install_mode'] and r['image']:
        if r['image'].lower().endswith('packages.conf'):
            r['install_mode'] = 'INSTALL'
        elif r['image'].lower().endswith('.bin'):
            r['install_mode'] = 'BUNDLE'
    return r


_STACK_HDR = re.compile(r'^\s*Switch\s+Ports\s+Model\s+SW Version\s+SW Image', re.M)
_STACK_ROW = re.compile(r'^\s*(\*)?\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(INSTALL|BUNDLE))?\s*$')
_MEMBER_HDR = re.compile(r'^\s*Switch\s+0*(\d+)\s*$')


def _parse_stack_table(text, result):
    """Catalyst stacks: member table plus per-member 'Switch 0N' blocks of key : value lines."""
    hdr = _STACK_HDR.search(text)
    if not hdr:
        return []
    members = {}
    after = text[hdr.end():].split('\n')
    started = False
    for line in after:
        if re.match(r'^\s*-{3,}', line):
            started = True
            continue
        m = _STACK_ROW.match(line)
        if m:
            started = True
            num = int(m.group(2))
            members[num] = {'number': num, 'active': bool(m.group(1)), 'model': m.group(4),
                            'sw_version': m.group(5), 'mode': m.group(7), 'serial': None}
            continue
        if started and not line.strip():
            break

    # Top-of-output Model/Serial belong to the switch we are logged into (the active one).
    top = text[:hdr.start()]
    active = [n for n, mem in members.items() if mem['active']]
    if active:
        members[active[0]]['serial'] = _first(r'^\s*System [Ss]erial [Nn]umber\s*:\s*(\S+)', top)
        result['install_mode'] = members[active[0]]['mode']

    current = None
    for line in text[hdr.end():].split('\n'):
        m = _MEMBER_HDR.match(line)
        if m:
            current = members.setdefault(int(m.group(1)), {'number': int(m.group(1)),
                                                            'active': False, 'model': None,
                                                            'sw_version': None, 'mode': None,
                                                            'serial': None})
            continue
        if current is None:
            continue
        kv = re.match(r'^\s*([A-Za-z][A-Za-z ]+?)\s*:\s*(\S.*?)\s*$', line)
        if kv:
            key = kv.group(1).lower()
            if key == 'system serial number':
                current['serial'] = kv.group(2)
            elif key == 'model number' and not current['model']:
                current['model'] = kv.group(2)
    return [members[n] for n in sorted(members)]


# --------------------------------------------------------------------------- show inventory

# The PID line is matched with [ \t] rather than \s so an empty VID or SN (common on
# fans and built-in modules) cannot run on into the next entry.
_INV_RE = re.compile(
    r'NAME:\s*"(?P<name>[^"]*)"\s*,\s*DESCR:\s*"(?P<descr>[^"]*)"[^\n]*\n'
    r'\s*PID:[ \t]*(?P<pid>[^,\n]*?)[ \t]*,[ \t]*VID:[ \t]*(?P<vid>[^,\n]*?)[ \t]*,'
    r'[ \t]*SN:[ \t]*(?P<sn>[^\s,]*)')


def parse_inventory(text):
    """Return (items, name_lines). name_lines lets the report flag entries we failed to pair."""
    items = []
    for m in _INV_RE.finditer(text):
        sn = real_serial(m.group('sn'))
        items.append({'name': m.group('name').strip(), 'descr': m.group('descr').strip(),
                      'pid': m.group('pid').strip(), 'vid': m.group('vid').strip(), 'serial': sn})
    name_lines = len(re.findall(r'^\s*NAME:\s*"', text, re.M))
    return items, name_lines


# --------------------------------------------------------------------------- show module

_MODULE_TABLES = [
    ('nx_main', re.compile(r'^\s*Mod\s+Ports\s+Module-Type\s+Model\s+Status')),
    ('xe_main', re.compile(r'^\s*Mod\s+Ports\s+Card Type\s+Model\s+Serial')),
    ('nx_sw', re.compile(r'^\s*Mod\s+Sw\s+Hw')),
    ('nx_mac', re.compile(r'^\s*Mod\s+MAC-Address\(es\)\s+Serial-Num')),
    ('xe_mac', re.compile(r'^\s*Mod\s+MAC addresses\s+Hw\s+Fw\s+Sw\s+Status')),
    ('xe_red', re.compile(r'^\s*Mod\s+Redundancy Role')),
    ('stack', re.compile(r'^\s*Switch\s+Ports\s+Model\s+Serial No')),
    ('nx_diag', re.compile(r'^\s*Mod\s+Online Diag Status')),
]


def parse_module(text):
    """Return {'unsupported', 'chassis_type', 'rows': {slot: {...}}, 'unparsed_rows': n}."""
    out = {'unsupported': is_unsupported(text), 'rows': {}, 'unparsed_rows': 0,
           'chassis_type': _first(r'^\s*Chassis Type\s*:\s*(\S+)', text)}
    table = None
    switch_prefix = ''
    for line in text.split('\n'):
        sw = re.match(r'^\s*Switch Number\s*:?\s*(\d+)', line)
        if sw:
            switch_prefix = sw.group(1) + '/'
            continue
        hit = None
        for name, rx in _MODULE_TABLES:
            if rx.match(line):
                hit = name
                break
        if hit:
            table = hit
            continue
        if re.match(r'^\s*Mod\b', line):
            table = None           # a table we do not use
            continue
        if not re.match(r'^\s*\d+\s', line) or table is None:
            continue
        tok = line.split()
        slot = switch_prefix + tok[0]
        row = out['rows'].setdefault(slot, {'slot': slot})
        try:
            if table == 'nx_main':
                rest = tok[2:]
                if rest and rest[-1] == '*':
                    rest = rest[:-1]
                row.update(ports=tok[1], status=rest[-1], model=rest[-2],
                           type=' '.join(rest[:-2]))
            elif table == 'xe_main':
                row.update(ports=tok[1], type=' '.join(tok[2:-2]), model=tok[-2], serial=tok[-1])
            elif table == 'nx_sw':
                row.update(sw=tok[1], hw=tok[2])
            elif table == 'nx_mac':
                if real_serial(tok[-1]):
                    row['serial'] = tok[-1]
            elif table == 'xe_mac':
                row.update(hw=tok[4], fw=tok[5], sw=tok[6], status=' '.join(tok[7:]))
            elif table == 'xe_red':
                row.update(role=tok[1])
            elif table == 'stack':
                row.update(ports=tok[1], model=tok[2], serial=tok[3], hw=tok[5], sw=tok[6])
            elif table == 'nx_diag':
                row.update(diag=tok[1])
        except IndexError:
            out['unparsed_rows'] += 1
    return out


# --------------------------------------------------------------------------- SNMP location

def parse_location(text):
    """'show snmp location' (IOS-XE prints just the string), NX-OS 'show snmp' ('sys location:')
    or IOS 'show snmp' ('Location: ...')."""
    m = (re.search(r'^\s*snmp-server location\s+(\S.*?)\s*$', text, re.M | re.I)
         or re.search(r'^\s*(?:sys )?location\s*:\s*(\S.*?)\s*$', text, re.M | re.I))
    if m:
        return m.group(1)
    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.strip().startswith('%')]
    return lines[0] if len(lines) == 1 else None


# --------------------------------------------------------------------------- FEX / VDC

# FEX states NX-OS prints in 'show fex' (longest first). Matching a known state - rather than
# relying on column spacing - keeps descriptions with spaces or odd text ('RACK-RU 12') intact.
FEX_STATES = ['AA Version Mismatch', 'Fabric Interface Down', 'Identity Mismatch', 'Online Sequence',
              'Offline Sequence', 'Image Download', 'Discovered', 'Registered', 'Configured',
              'Connected', 'Offlining', 'Online', 'Offline', 'Unknown', 'Invalid', 'Down', 'Up']


def parse_fex(text):
    """NX-OS 'show fex' -> {number: {'description', 'state', 'model', 'serial'}}.
    Rows are read from the right: [model [serial]] after a known state; the description is
    whatever sits between the number and the state."""
    out = {}
    states = [s.lower().split() for s in FEX_STATES]
    for line in text.split('\n'):
        tok = line.split()
        if len(tok) < 2 or not re.match(r'^\d{3,4}$', tok[0]):
            continue
        found = None
        for i in range(len(tok) - 1, 0, -1):            # rightmost state that leaves <= 2 tokens
            for words in states:
                j = i + len(words)
                if [t.lower() for t in tok[i:j]] == words and len(tok) - j <= 2:
                    found = (i, j)
                    break
            if found:
                break
        if found:
            i, j = found
            after = tok[j:]
            out[tok[0]] = {'description': ' '.join(tok[1:i]), 'state': ' '.join(tok[i:j]),
                           'model': after[0] if after else '', 'serial': after[1] if len(after) > 1 else ''}
            continue
        m = re.match(r'^\s*(\d{3,4})\s+(\S.*?)\s{2,}(\S.*?)\s{2,}(\S+)\s+(\S+)\s*$', line)
        if m and not _RACK_RU.search(m.group(3)):      # unknown state word: column spacing
            out[m.group(1)] = {'description': m.group(2), 'state': m.group(3),
                               'model': m.group(4), 'serial': m.group(5)}
        else:                                             # number + description only
            out[tok[0]] = {'description': ' '.join(tok[1:]), 'state': '', 'model': '', 'serial': ''}
    return out


# Description text such as a rack-RU ('ZZ30-41') must never be taken for a FEX state.
_RACK_RU = re.compile(r'\S+-\d{1,2}(?:\s|$)')


def parse_vdc(text):
    """NX-OS 'show vdc' -> list of (id, name, type)."""
    out = []
    for line in text.split('\n'):
        m = re.match(r'^\s*(\d+)\s+(\S+)\s+(active|failed|suspended|creating|\S+)\s+\S+\s+(\S+)', line)
        if m:
            out.append((m.group(1), m.group(2), m.group(4)))
    return out


__all__ = ['parse_version', 'parse_inventory', 'parse_module', 'parse_location', 'parse_fex',
           'parse_vdc', 'is_unsupported']
