"""The manual inventory sheet: hardware typed in by hand, for networks SolarWinds cannot
collect from at all (isolated networks), bench stock, and devices it never reaches.

One row per part. Rows with the same Hostname form one device (a chassis or stack plus its
cards, PSUs ...). Rows with no Hostname are loose stock, grouped by Location ('STOCK - CAGE 2').
The sheet may hold several networks (Network column); blank Network = the network chosen at
import. Rows without a serial number are left out, like everywhere else, and listed on Issues.

    write_xlsx(path, template_sheets())            blank sheet (Excel, Text-format columns)
    read_manual(path, 'BENCH') -> ({network: [devices]}, info)
"""

import csv
import os

from .kinds import kind_of, slot_of, member_of, KIND_LABELS
from .model import new_device, drop_unserialized, check_duplicate_serials
from .roles import role_from_model
from .textutil import norm_host, read_text, real_serial
from .xlsx import read_xlsx

COLUMNS = ['Network', 'Location', 'Hostname', 'Unit', 'Kind', 'PID', 'Serial', 'Description',
           'Status', 'Mgmt IP', 'Software', 'Notes']
# Kind drop-down (label -> kind). Blank = worked out from the PID / description.
KIND_CHOICES = [('Chassis', 'chassis'), ('Stack member', 'stack_member'), ('FEX', 'fex'),
                ('Supervisor', 'supervisor'), ('Line card', 'linecard'), ('Fabric module', 'fabric'),
                ('System controller', 'sysctrl'), ('Module', 'module'), ('Power supply', 'power'),
                ('Fan', 'fan'), ('Optic', 'transceiver'), ('Stack cable', 'stack_cable'),
                ('Harddisk / SSD', 'storage'), ('Acknowledged', 'acknowledged'), ('Other', 'other')]
STATUS_CHOICES = ['Installed', 'Spare', 'RMA', 'Loaner', 'Failed']

_KIND_WORDS = dict((label.lower(), kind) for label, kind in KIND_CHOICES)
_KIND_WORDS.update((label.lower(), kind) for kind, label in KIND_LABELS.items())
_KIND_WORDS.update((kind, kind) for _, kind in KIND_CHOICES)
_KIND_WORDS.update({'switch': 'stack_member', 'member': 'stack_member', 'router': 'chassis',
                    'psu': 'power', 'power': 'power', 'sup': 'supervisor', 'linecard': 'linecard',
                    'card': 'linecard', 'sfp': 'transceiver', 'transceiver': 'transceiver',
                    'fabric extender': 'fex', 'nim': 'module', 'uplink module': 'module',
                    'stack cable': 'stack_cable', 'cable': 'stack_cable', 'ssd': 'storage',
                    'harddisk': 'storage', 'storage': 'storage', 'acknowledged': 'acknowledged'})

# Header names accepted for each column (lower case).
_ALIASES = {
    'network': ('network',),
    'location': ('location', 'site', 'building', 'room', 'rack', 'bin', 'shelf'),
    'hostname': ('hostname', 'host', 'device', 'device name', 'node'),
    'unit': ('unit', 'slot', 'position', 'name', 'inventory name'),
    'kind': ('kind', 'type', 'category'),
    'pid': ('pid', 'part number', 'part', 'product id', 'model', 'model / pid'),
    'serial': ('serial', 'serial number', 'sn', 's/n', 'serial no', 'serial no.'),
    'descr': ('description', 'descr'),
    'status': ('status', 'state', 'condition'),
    'ip': ('mgmt ip', 'ip', 'ip address', 'management ip'),
    'version': ('software', 'version', 'software version'),
    'notes': ('notes', 'note', 'comment', 'comments'),
}

HELP = [
    ('Network', 'Which network the row belongs to (e.g. SITE-C, BENCH). Blank = the network you '
                'pick when importing. One sheet can hold several networks.'),
    ('Location', 'Where it is: room / rack for installed gear, shelf / cage / bin for stock.'),
    ('Hostname', 'The device the part is in. Every row of one device uses the same hostname. '
                 'Leave blank for loose stock (spares on a shelf): those are grouped by Location.'),
    ('Unit', 'Which part of the device: "Switch 1", "Switch 2", "Slot 3", "Power Supply 1". Use '
             '"Switch N ..." for everything in stack member N (e.g. "Switch 2 Uplink Module").'),
    ('Kind', 'Pick from the list. Blank = NetInv works it out from the PID.'),
    ('PID', 'Cisco part number, exactly as on the label / show inventory (C9300-48P, C9300-NM-8X).'),
    ('Serial', 'Serial number. REQUIRED: rows without one are left out and listed on Issues.'),
    ('Description', 'Optional.'),
    ('Status', 'Installed / Spare / RMA / Loaner / Failed (or your own word). Blank = Installed for '
               'rows with a hostname, Spare for loose stock.'),
    ('Mgmt IP, Software', 'Optional, per device (any one row of the device).'),
    ('Notes', 'Anything else. Shown on the Hardware sheet.'),
    ('', ''),
    ('Example', 'SITE-C | Bldg 4 Rm 12 | SITE-C-AS1 | Switch 1 | Stack member | C9300-48P | FOC2233X0AB'),
    ('Example', 'SITE-C | Bldg 4 Rm 12 | SITE-C-AS1 | Switch 1 Uplink Module | Module | C9300-NM-8X | FOC2233X1CD'),
    ('Example', 'BENCH | Cage 2 shelf B |  |  | Power supply | PWR-C1-715WAC | DCB2240A1B2 | ... | Spare'),
    ('', ''),
    ('Re-import', 'Edit the sheet and import it again whenever things change: the network\'s manual '
                  'rows are replaced by the sheet, and the Changes sheet lists what moved. Keep this '
                  'file - it is the record for these networks.'),
]


def template_sheets(networks=None):
    """Sheets for write_xlsx: the Inventory sheet (an Excel Table with Text-format columns and
    drop-downs) and a 'How to fill' sheet. networks: optional [(name, devices)] whose manual
    devices are written as rows, to edit and re-import."""
    rows = []
    for name, devices in networks or ():
        for d in devices:
            if d.get('source') == 'manual':
                rows.extend(device_rows(name, d))
    lists = {COLUMNS.index('Kind'): [label for label, _ in KIND_CHOICES],
             COLUMNS.index('Status'): STATUS_CHOICES}
    return [('Inventory', COLUMNS, rows, {'table': 'ManualInventory', 'text_columns': True, 'lists': lists}),
            ('How to fill', ['Column', 'What to enter'], [list(r) for r in HELP])]


def device_rows(network, d):
    labels = dict((kind, label) for label, kind in KIND_CHOICES)
    host = '' if d.get('stock') else d['hostname']
    return [[network, d['location'], host, it['name'], labels.get(it['kind'], 'Other'), it['pid'],
             it['serial'], it['descr'], it['role'], d['mgmt_ip'], d['version'], it.get('notes', '')]
            for it in d['hardware']]


# --------------------------------------------------------------------------- reading

def _table(path):
    """(rows, sheet name) of the first sheet with a header row naming both PID and Serial."""
    if path.lower().endswith(('.xlsx', '.xlsm')):
        sheets = read_xlsx(path)
    else:
        sheets = [(os.path.basename(path), list(csv.reader(read_text(path).splitlines())))]
    for name, rows in sheets:
        for i, row in enumerate(rows[:15]):
            cells = [c.strip().lower() for c in row]
            if any(c in _ALIASES['pid'] for c in cells) and any(c in _ALIASES['serial'] for c in cells):
                return rows[i:], name, i
    raise ValueError('%s: no header row with PID and Serial columns found' % os.path.basename(path))


def read_manual(path, default_network=''):
    """-> ({network: [devices]}, info). info: rows read, rows used, problems [(row, text)]."""
    rows, sheet, offset = _table(path)
    header = [c.strip().lower() for c in rows[0]]
    col = {}
    for field, names in _ALIASES.items():
        for n in names:
            if n in header:
                col[field] = header.index(n)
                break
    info = {'sheet': sheet, 'rows': 0, 'used': 0, 'problems': [],
            'columns': sorted(rows[0][i] for i in col.values())}
    nets = {}                                    # network -> {device key: device}

    def get(row, field):
        i = col.get(field)
        return row[i].strip() if i is not None and i < len(row) and row[i] else ''

    for n, row in enumerate(rows[1:], offset + 2):          # Excel row numbers
        if not any(c.strip() for c in row):
            continue
        info['rows'] += 1
        network = get(row, 'network') or default_network
        if not network:
            info['problems'].append((n, 'no Network and no default network - row skipped'))
            continue
        host, location = get(row, 'hostname'), get(row, 'location')
        if host:
            key = norm_host(host)
        else:
            key = 'stock:' + location.lower()
            host = 'STOCK - %s' % location if location else 'STOCK'
        devices = nets.setdefault(network, {})
        d = devices.get(key)
        if d is None:
            d = devices[key] = new_device(key, source='manual')
            d.update(hostname=host, stock=key.startswith('stock:'), commands={'manual'},
                     host_sources={'manual sheet'}, files={os.path.basename(path)})
        d['location'] = d['location'] or location
        d['mgmt_ip'] = d['mgmt_ip'] or get(row, 'ip')
        d['version'] = d['version'] or get(row, 'version')

        unit, pid, descr = get(row, 'unit'), get(row, 'pid'), get(row, 'descr')
        kind_text = get(row, 'kind')
        kind = _KIND_WORDS.get(kind_text.lower()) if kind_text else None
        if kind_text and not kind:
            d['warnings'].append('Row %d: unknown kind "%s" - worked out from the PID' % (n, kind_text))
        if not kind:
            kind = _kind_from_pid(unit, descr, pid)
        serial = real_serial(get(row, 'serial'))
        label = unit or pid or 'row %d' % n
        if not serial:
            d['warnings'].append('Row %d: %s %s has no serial number - left out' % (n, label, pid))
            continue
        if any(it['serial'] == serial for it in d['hardware']):
            d['warnings'].append('Row %d: serial %s is listed twice - kept once' % (n, serial))
            continue
        item = {'kind': kind, 'kind_given': bool(kind_text and _KIND_WORDS.get(kind_text.lower()) not in (None, 'other')),
                'name': unit or pid, 'descr': descr, 'pid': pid, 'vid': '',
                'serial': serial, 'status': '', 'sw': '', 'notes': get(row, 'notes'), 'row': n,
                'role': get(row, 'status') or ('Spare' if d['stock'] else 'Installed'),
                'slot': slot_of({'name': unit}), 'member': member_of({'name': unit})}
        if kind == 'stack_member' and not item['slot'] and not d['stock']:   # 'Switch', no number
            item['slot'] = str(1 + sum(1 for it in d['hardware'] if it['kind'] == 'stack_member'))
            item['name'] = unit or 'Switch %s' % item['slot']
        d['hardware'].append(item)
        info['used'] += 1

    out = {}
    for network, devices in nets.items():
        for d in devices.values():
            drop_unserialized(d)
            if d['stock']:                       # a shelf, not a stack: no model of its own
                d.update(members=[], model='', serial='')
                continue
            if d['members'] and not any(m['role'] == 'active' for m in d['members']):
                d['members'].sort(key=lambda m: int(m['slot']) if m['slot'].isdigit() else 0)
                d['serial'] = d['members'][0]['serial']
        check_duplicate_serials(devices)
        out[network] = sorted(devices.values(), key=lambda d: (d.get('stock', False), d['hostname']))
    return out, info


def _kind_from_pid(unit, descr, pid):
    """Kind for a row with Kind left blank: the inventory rules, then the model family
    (a bare 'C9300-48P' is a switch: a stack member, as show inventory lists it; a C8300 or
    Nexus is a chassis)."""
    kind = kind_of({'name': unit, 'descr': descr, 'pid': pid})
    if kind in dict((k, 1) for _, k in KIND_CHOICES) and kind != 'other':
        return kind
    role = role_from_model(pid, local=False)       # stored in the snapshot: no local rules
    if role in ('access', 'sw'):
        return 'stack_member'
    return 'chassis' if role else 'other'


def manual_stats(info):
    """build_stats / split_stats shaped like an exports import, for the summary report."""
    build = {'sections': {'manual rows': info['rows']}, 'unsupported': {}, 'inventory_name_lines': info['rows'],
             'inventory_items': info['used'], 'kinds': {}, 'unrecognised': {}, 'version_missing_lines': [],
             'module_unparsed': 0, 'unidentified_sections': 0, 'vdc_groups': 0, 'no_serial': {}}
    split = {'files_seen': 1, 'files_used': 1, 'files_skipped': 0, 'bytes': 0,
             'layouts': {'manual inventory sheet': 1}, 'skipped_reasons': {}}
    return build, split

