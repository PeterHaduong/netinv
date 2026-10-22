"""The show commands to put in the SolarWinds NCM "Execute Command Script" job.

One list for every platform: a device that does not support a command answers with an
error ('% Invalid input', '% Invalid command', 'not enabled' ...), which the parser
records as 'not supported' and skips. So the same script runs on IOS, IOS-XE and NX-OS.

The script starts with 'terminal length 0' (no --More-- paging when the list is pasted into an
SSH session by hand) and puts a '!' line between commands: '!' is a comment to the device, and
the pause stops a pasted command (show inventory especially) from being lost while the previous
one is still printing. NetInv skips both in the output.
"""

SCRIPT_START = 'terminal length 0'
SEPARATOR = '!'

# (command, what it is used for, where it works, included by default)
CATALOG = [
    ('show version', 'Hostname, software version, model, serial, uptime, boot mode',
     'all', True),
    ('show inventory', 'Chassis, stack members, modules, FEX, power supplies, fans, serials',
     'all', True),
    ('show module', 'Module status, redundancy role and software per slot',
     'C9300/C9400 and NX-OS (C8300/ISR reject it - skipped)', True),
    ('show fex', 'FEX name and state',
     'NX-OS FEX parents (others reject it - skipped)', True),
    ('show running-config | include snmp-server location', 'SNMP location column',
     'all', True),
    ('show vdc', 'VDC names on the Nexus 7700 (one chassis, counted once)',
     'NX-OS (IOS rejects it - skipped)', True),
]


def default_commands():
    return [c for c, _, _, on in CATALOG if on]


def script_text(commands):
    """The command list ready to paste into an NCM command script or an SSH session:
    'terminal length 0' first, a '!' line between commands."""
    lines = [SCRIPT_START]
    for c in commands:
        lines += [SEPARATOR, c]
    return '\n'.join(lines) + '\n'
