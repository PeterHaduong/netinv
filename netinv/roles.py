"""Device roles (the workbook's Role column) from the local hostname convention. This is the
file to edit when the naming convention changes.

Hostnames are split on - _ . into tokens and trailing digits are ignored, so
'CAMPUS-1A-AS1(2)' -> ['CAMPUS', '1A', 'AS'] -> access. Rules are checked in order and
the first rule with a matching token wins (so 'PER-DMZ-SW1' is DMZ, not a plain switch).
"""

import re

# (role, tokens) - from the team's convention, plus a few common extras.
ROLE_TOKENS = [
    ('border', ['BL']),                    # border leaf
    ('spine', ['SP']),
    ('leaf', ['LF']),
    ('fex', ['FEX']),
    ('firewall', ['FW']),
    ('dmz', ['DMZ']),
    ('br', ['BR']),                        # border router (perimeter)
    ('xr', ['XR', 'ROUTER', 'RTR']),       # external router
    ('xts', ['XTS']),                      # external transport switch
    ('core', ['CS']),                      # core switch
    ('dist', ['DS', 'DIST']),
    ('access', ['AS', 'ACCESS']),
    ('sw', ['SW']),                        # switch (L2)
]
# Used when no hostname token matches: (role, regex on the model / PID).
MODEL_RULES = [
    ('voice', r'(?i)^VG310(?:[-\s]|$)'),
    ('xr', r'^(C8[0-9]{3}|C8500|C1[01]\d\d|ISR|ASR|CISCO\d)'),
    ('core', r'^(N77|N7K)'),
    ('spine', r'^N9K-C95'),
    ('leaf', r'^N9K-C93'),
    ('dist', r'^(C94\d\dR?|C9500)'),
    ('access', r'^C93\d\d'),                # C9300, C9300X/L, C9350
    ('sw', r'^(C9200|C38\d\d|WS-C|C1000)'),
]

LABEL = {'br': 'Border router', 'xr': 'External router', 'xts': 'External transport switch',
         'core': 'Core switch', 'spine': 'Spine', 'dist': 'Distribution', 'leaf': 'Leaf',
         'border': 'Border leaf', 'access': 'Access switch', 'fex': 'Fabric extender',
         'sw': 'Switch (L2)', 'firewall': 'Firewall', 'dmz': 'DMZ switch',
         'voice': 'Analog voice gateway'}
_EXTRA = []


def set_extra_rules(rules):
    """Rules from settings.json "role_rules": [[role, model regex], ...], e.g. ["access",
    "^C9350"], checked before MODEL_RULES (the hostname still wins). role is one of LABEL's
    keys or any other text, which is then shown as the label. Returns a list of problems."""
    good, problems = [], []
    for r in rules or []:
        try:
            role, rx = r
            good.append((str(role), re.compile(rx)))
        except (TypeError, ValueError, re.error) as exc:
            problems.append('role_rules %r: %s' % (r, exc))
    _EXTRA[:] = good
    return problems


def tokens(hostname):
    name = re.sub(r'\(.*\)$', '', hostname or '').split('.')[0]
    return [re.sub(r'\d+$', '', t).upper() for t in re.split(r'[-_]', name) if t]


def role_from_hostname(hostname):
    toks = set(tokens(hostname))
    for role, words in ROLE_TOKENS:
        if toks.intersection(words):
            return role
    return ''


def role_from_model(model):
    for role, rx in _EXTRA + MODEL_RULES:
        if re.search(rx, model or ''):
            return role
    return ''


def role_label(hostname, model=''):
    role = role_from_hostname(hostname) or role_from_model(model)
    return LABEL.get(role, role)
