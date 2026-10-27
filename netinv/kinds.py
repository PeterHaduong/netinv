"""Sort 'show inventory' entries into hardware kinds (chassis, supervisor, fan ...).

Rules are plain data, checked top to bottom; the first match wins. Part-number
(PID) rules come before description keywords because descriptions mislead, e.g.
a C9400-LC-24XS line card's description contains "(SFP+)".

Local rules can be added without editing this file: settings.json "kind_rules" (the app's
Rules... dialog, see set_extra_rules). They are applied when a snapshot is loaded, so the stored
data always holds this file's classification and removing a rule undoes it.
"""

import re

# Kinds the inventory counts as "modules" (field-replaceable boards).
MODULE_KINDS = ('supervisor', 'linecard', 'fabric', 'sysctrl', 'module')
# Kinds listed but not counted as modules.
OTHER_KINDS = ('chassis', 'stack_member', 'fex', 'power', 'fan', 'builtin', 'other')
# Kinds never listed. Optics ('transceiver') are kept but hidden unless the workspace setting
# include_optics is on (model.hide_optics).
EXCLUDED_KINDS = ('stack',)
# Order of kinds in lists and the Part counts sheet.
KIND_ORDER = ('chassis', 'stack_member', 'fex', 'supervisor', 'linecard', 'fabric', 'sysctrl',
              'module', 'power', 'fan', 'transceiver', 'builtin', 'other')

KIND_LABELS = {
    'chassis': 'Chassis', 'stack_member': 'Stack member', 'fex': 'Fabric extender (FEX)', 'supervisor': 'Supervisor',
    'linecard': 'Line card', 'fabric': 'Fabric module', 'sysctrl': 'System controller',
    'module': 'Module (NIM/SM/uplink)', 'power': 'Power supply', 'fan': 'Fan',
    'transceiver': 'Optic / transceiver', 'stack': 'Stack (logical)',
    'builtin': 'Built-in (part of chassis)', 'other': 'Unrecognised',
}

_INTF_NAME = (r'^(Gi|Te|Fo|Hu|Twe|Tw|Fi|Ap|Et|Eth|Ethernet|GigabitEthernet|TenGigabitEthernet|'
              r'TwentyFiveGigE|FortyGigabitEthernet|HundredGigE|FiftyGigE|TwoGigabitEthernet|'
              r'FiveGigabitEthernet|AppGigabitEthernet)\s*\d+(/\d+)*$')

# (kind, field, regex)  field is 'name', 'descr', 'pid' or 'any' (name + descr)
RULES = [
    ('stack', 'name', r'(?i)\bstack$'),
    ('chassis', 'name', r'(?i)^(chassis|rack)(\s*\d+)?$'),
    # Cisco VG310: the chassis is named after the model; its FXS ports are built in
    ('chassis', 'name', r'(?i)^VG310$'),
    ('builtin', 'name', r'(?i)^onboard \d+ FXS voice interface on Slot \d+ SubSlot \d+$'),
    ('stack_member', 'name', r'(?i)^(switch\s*)?\d+$'),
    ('builtin', 'name', r'(?i)^module\s+[RF]\d+$'),
    ('builtin', 'name', r'(?i)^NIM subslot 0/\d+$'),     # ISR4K / C8300 front-panel ports
    ('fex', 'name', r'(?i)^FEX\s*\d+\s+CHASSIS$'),
    ('builtin', 'name', r'(?i)^FEX\s*\d+\s+Module\s*\d+$'),
    # --- part-number families ---
    ('supervisor', 'pid', r'^(C9[46]00X?-SUP|C9410-SUP|N9K-SUP|N7K-SUP|N77-SUP|WS-SUP|WS-X45-SUP)'),
    ('fabric', 'pid', r'^(N9K-C95\d\d-FM|N77-C77\d\d-FAB|N7K-C70\d\d-FAB)'),
    ('sysctrl', 'pid', r'^N9K-SC-'),
    ('linecard', 'pid', r'^(C9400X?-LC|C9600X?-LC|N9K-X9[0-9]|N7K-[MF]\d|N77-[MF]\d|WS-X4\d|WS-X6\d)'),
    ('power', 'pid', r'^(PWR-|C9400-PWR|N9K-P[AD]C|N9K-PUV|NXA-P[AD]C|N77-AC|N77-DC|N7K-AC|N2200-P|C9K-PWR|C8300-PWR|'
                     r'PWR-CC\d|PWR-4\d{3}|ASR\d+-PWR)'),
    ('fan', 'pid', r'^(C94\d\dR?-FAN|C9400-FAN|N9K-C95\d\d-FAN|N77-C77\d\d-FAN|N7K-C70\d\d-FAN|NXA-FAN|N2K-C2\d+-FAN|C9K-T\d-FAN|FAN-|C8300-FAN|'
                   r'C9300-FAN)'),
    ('module', 'pid', r'^(C-?NIM-|NIM-|SM-X-|C-SM-|SM-|NM-|EHWIC|HWIC|C9[23]00X?-NM|C3850-NM|'
                      r'C3KX-NM|PVDM|C9300-NM)'),
    ('transceiver', 'pid', r'^(SFP|GLC|QSFP|CVR|CFP|XFP|X2-|XENPAK|DWDM-|CWDM-|ONS-S|DS-SFP|'
                           r'MA-SFP|QDD-|Q28-|SFP28|10G-|1000BASE)'),
    # --- name patterns ---
    ('transceiver', 'name', r'(?i)transceiver'),
    ('transceiver', 'name', _INTF_NAME),
    ('power', 'any', r'(?i)power supply|\bPSU\b|\bPEM\b'),
    ('fan', 'any', r'(?i)\bfan\b'),
    ('supervisor', 'any', r'(?i)supervisor|route processor|\bRSP\b'),
    ('fabric', 'any', r'(?i)fabric module'),
    ('sysctrl', 'any', r'(?i)system controller'),
    ('linecard', 'any', r'(?i)line ?card|linecard|ethernet module'),
    ('module', 'name', r'(?i)^(module\s+\S+|NIM subslot|SM subslot|subslot|.*uplink module)'),
    ('module', 'descr', r'(?i)\bmodule\b'),
    ('transceiver', 'descr', r'(?i)\bSFP\b|\bQSFP|GBIC|XFP|BASE-|Gbase'),
]
_COMPILED = [(kind, field, re.compile(rx)) for kind, field, rx in RULES]
_EXTRA = []
FIELDS = ('pid', 'name', 'descr', 'any')
# What a local rule may make a part. Never chassis / stack member / FEX: which box is which
# is decided by the parser, a rule only names parts.
RULE_KINDS = ('supervisor', 'linecard', 'fabric', 'sysctrl', 'module', 'power', 'fan',
              'transceiver', 'builtin', 'other')
BOX_KINDS = ('chassis', 'stack_member', 'fex', 'stack')


def set_extra_rules(rules):
    """Rules from settings.json "kind_rules": [[kind, field, regex], ...], e.g.
    ["power", "pid", "^C9350-PWR"]. Checked before the built-in rules, for parts only (see
    RULE_KINDS). Returns a list of problems (bad entries are skipped)."""
    good, problems = [], []
    for r in rule_list(rules, 'kind_rules', problems):
        try:
            good.append(compile_rule(r))
        except (TypeError, ValueError, re.error) as exc:
            problems.append('kind_rules %r: %s' % (r, exc))
    _EXTRA[:] = good
    return problems


def rule_list(rules, name, problems=None):
    """The settings value as a list ('kind_rules': 5 or a string is a mistake, not a crash)."""
    if rules in (None, ''):
        return []
    if not isinstance(rules, list):
        if problems is not None:
            problems.append('%s must be a list of rules, not %r' % (name, rules))
        return []
    return rules


def valid_rules(rules):
    """The kind_rules entries that are actually applied (for the About sheet)."""
    out = []
    for r in rule_list(rules, 'kind_rules'):
        try:
            compile_rule(r)
            out.append(list(r))
        except (TypeError, ValueError, re.error):
            pass
    return out


def compile_rule(r):
    """[kind, field, regex] -> (kind, field, compiled). Raises ValueError / re.error."""
    if not isinstance(r, (list, tuple)) or len(r) != 3:
        raise ValueError('a rule is [kind, field, pattern]')
    kind, field, rx = r
    if not isinstance(kind, str) or kind not in RULE_KINDS:
        raise ValueError('kind must be one of %s' % ', '.join(RULE_KINDS))
    if not isinstance(field, str) or field not in FIELDS:
        raise ValueError('field must be one of %s' % ', '.join(FIELDS))
    if not isinstance(rx, str) or not rx.strip():
        raise ValueError('empty pattern')
    return (kind, field, re.compile(rx))


def rule_hits(rule, item):
    """Does one compiled rule match this item?"""
    return _match([rule], item) is not None


def extra_kind(item):
    """The kind from the settings rules, or '' (none matches, or the item is a box)."""
    if item.get('kind') in BOX_KINDS:
        return ''
    return _match(_EXTRA, item) or ''


def kind_of(item):
    """Return the kind for one inventory item dict (name/descr/pid)."""
    return _match(_COMPILED, item) or 'other'


def _match(rules, item):
    name, descr, pid = item.get('name', ''), item.get('descr', ''), item.get('pid', '')
    for kind, field, rx in rules:
        if field == 'name':
            hit = rx.search(name)
        elif field == 'descr':
            hit = rx.search(descr)
        elif field == 'pid':
            hit = rx.search(pid)
        else:
            hit = rx.search(name) or rx.search(descr)
        if hit:
            return kind
    return None


def slot_of(item):
    """Chassis slot number from the inventory NAME ('Slot 3 Linecard' -> '3', 'Switch 2' -> '2',
    'NIM subslot 0/1' -> '0/1'). Empty for PSUs, fans and fabric cards, whose numbers are not
    chassis slots - so they are never matched against 'show module' slot rows."""
    name = item.get('name', '')
    m = re.match(r'(?i)^(?:switch\s*)?(\d+)$', name)
    if m:
        return m.group(1)
    m = re.match(r'(?i)^slot\s*(\d+)\b', name)
    if m:
        return m.group(1)
    m = re.search(r'(?i)\bsubslot\s+(\d+/\d+)$', name)
    if m:
        return m.group(1)
    return ''


def member_of(item):
    """Stack member number an item belongs to ('Switch 2 - Power Supply A' -> '2')."""
    m = re.match(r'(?i)^(switch|fex)\s*(\d+)\b', item.get('name', ''))
    if not m:
        return ''
    return m.group(2) if m.group(1).lower() == 'switch' else 'FEX ' + m.group(2)
