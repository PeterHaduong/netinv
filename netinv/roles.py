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
    ('xr', r'^(C8[0-9]{3}|C8500|C1[01]\d\d|ISR|ASR|CISCO\d)'),
    ('core', r'^(N77|N7K)'),
    ('spine', r'^N9K-C95'),
    ('leaf', r'^N9K-C93'),
    ('dist', r'^(C94\d\dR?|C9500)'),
    ('access', r'^C9300'),
    ('sw', r'^(C9200|C38\d\d|WS-C|C1000)'),
]

LABEL = {'br': 'Border router', 'xr': 'External router', 'xts': 'External transport switch',
         'core': 'Core switch', 'spine': 'Spine', 'dist': 'Distribution', 'leaf': 'Leaf',
         'border': 'Border leaf', 'access': 'Access switch', 'fex': 'Fabric extender',
         'sw': 'Switch (L2)', 'firewall': 'Firewall', 'dmz': 'DMZ switch'}


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
    for role, rx in MODEL_RULES:
        if re.search(rx, model or ''):
            return role
    return ''


def role_label(hostname, model=''):
    return LABEL.get(role_from_hostname(hostname) or role_from_model(model), '')
