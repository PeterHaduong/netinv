"""Split raw export files into per-device, per-command sections.

We do not know exactly how SolarWinds/NCM exports will look on every network, so
three layouts are handled and the dry-run report says which one was found:

  1. Terminal-style captures with prompt echoes:  CORE-1#show version
  2. One file per device (or per device+command) without prompts, where the
     command is recognised from its content ("signature").
  3. Several devices in one prompt-less file: split at each 'show version'.

Each section records *how* it was recognised and *where* its device name came
from, so problems can be diagnosed without anyone sharing the raw text.
"""

import os
import re

from .textutil import read_text, looks_binary, is_ipv4, norm_host, split_caption, IPV4_RE

MAX_FILE_BYTES = 50 * 1024 * 1024
SKIP_EXTS = {'.zip', '.7z', '.gz', '.exe', '.dll', '.xlsx', '.xls', '.docx', '.pdf',
             '.vsdx', '.vsd', '.png', '.jpg', '.csv', '.py', '.pyc', '.json'}

# Canonical commands we parse. An echoed command matches when every typed word is
# an abbreviation of the canonical word ("sh inv" -> show inventory). Anything else
# (e.g. CDP or routing commands left in an older SolarWinds job) becomes 'unknown' and is skipped.
COMMANDS = [
    ('version', ['show', 'version']),
    ('inventory', ['show', 'inventory']),
    ('module', ['show', 'module']),
    ('location', ['show', 'snmp', 'location']),
    ('location', ['show', 'snmp']),
    ('fex', ['show', 'fex']),
    ('vdc', ['show', 'vdc']),
]

PROMPT_RE = re.compile(r'^\s*(?P<host>[A-Za-z0-9][\w.\-]{0,62})(?:\([\w\-]+\))?[#>]\s?(?P<cmd>.*)$')

# Lines that start a command's output when there is no prompt echo. 'neighbors' and 'ignored'
# are outputs NetInv does not use; they are still recognised so that they end the previous
# section (and a CDP/LLDP neighbour's version banner is not taken for a 'show version').
SIGNATURES = [
    ('version', re.compile(r'^\s*Cisco IOS XE Software, Version\b'), True),
    ('version', re.compile(r'^\s*Cisco Nexus Operating System \(NX-OS\) Software\s*$'), True),
    ('version', re.compile(r'^\s*Cisco IOS Software\b'), False),
    ('version', re.compile(r'^\s*Cisco Internetwork Operating System Software\s*$'), False),
    ('inventory', re.compile(r'^\s*NAME:\s*"'), True),
    ('module', re.compile(r'^\s*Chassis Type\s*:'), True),
    ('module', re.compile(r'^\s*Mod\s+Ports\s+'), True),
    ('module', re.compile(r'^\s*Switch\s+Ports\s+Model\s+Serial No'), True),
    ('fex', re.compile(r'^\s*FEX\s+FEX\s+FEX'), True),
    ('vdc', re.compile(r'^\s*vdc_id\s+vdc_name'), True),
    ('neighbors', re.compile(r'^\s*(Device ID\s*:|Local Intf\s*:|Chassis id\s*:)'), True),
    ('ignored', re.compile(r'^\s*(Interface\s+Status\s+Protocol\s+Description|Port\s+Type\s+Speed\s+Description)'), True),
    ('ignored', re.compile(r'^\s*(Interface\s+IP-Address\s+OK\?|IP Interface Status for VRF)'), True),
    ('ignored', re.compile(r'^\s*(Neighbor ID\s+Pri\s+State|Codes: L - local|IP Route Table for VRF)'), True),
    ('ignored', re.compile(r'^\s*(Protocol\s+Address\s+Age|IP ARP Table|Mac Address Table|VLAN\s+MAC Address\s+Type)'), True),
    ('ignored', re.compile(r'^interface \S+'), True),
]
# Inside CDP/LLDP detail, a neighbour's own version banner follows these lines.
_NEIGHBOR_VERSION_LEAD = re.compile(r'^\s*(Version|System Description)\s*:\s*$')

_HOST_FROM_VERSION = [
    re.compile(r'^\s*Device name:\s*(\S+)', re.M),
    re.compile(r'^(?!Kernel |System |Uptime )(\S+)\s+uptime is\s', re.M),
]

_NAME_NOISE = {
    'show', 'sh', 'version', 'ver', 'inventory', 'inv', 'module', 'modules', 'mod', 'cdp',
    'lldp', 'neighbors', 'neighbor', 'nei', 'detail', 'det', 'running', 'startup', 'config',
    'running-config', 'startup-config', 'output', 'outputs', 'export', 'results', 'result',
    'ncm', 'solarwinds', 'script', 'txt', 'log', 'entry', 'fex', 'interface', 'interfaces',
    'int', 'description', 'descriptions', 'desc', 'status', 'vdc', 'snmp', 'location', 'brief',
    'summary', 'port-channel', 'etherchannel', 'all', 'detail',
}


# A '!' separator line from the command script, echoed bare or after a prompt ('CORE-1#!').
_SEPARATOR_LINE = re.compile(r'^\s*(?:[A-Za-z0-9][\w.\-]*(?:\([\w\-]+\))?[#>])?\s*!\s*$')


def _drop_separators(text):
    return '\n'.join(l for l in text.split('\n') if not _SEPARATOR_LINE.match(l))


class Section(object):
    """One command's output for one device ('!' separator lines from the script removed)."""

    def __init__(self, path, cmd, text, how, host=None, host_source=None, ip_hint=None, note='',
                 block=None):
        self.path = path
        self.cmd = cmd                  # 'version', 'inventory', ... or 'unknown' / 'ignored'
        self.text = _drop_separators(text)
        self.how = how                  # 'prompt' or 'signature'
        self.host = host
        self.host_source = host_source  # 'prompt', 'show version', 'file name', 'folder name'
        self.ip_hint = ip_hint
        self.note = note                # SolarWinds caption note: 'campus-as1 (BLDG 2)' -> 'BLDG 2'
        self.block = block              # which node block of which file it came from

    def __repr__(self):
        return 'Section(%s, %s, host=%s via %s)' % (os.path.basename(self.path), self.cmd,
                                                    self.host, self.host_source)


def match_command(cmd_text):
    """Map typed command text to a canonical key, or None."""
    if 'snmp-server location' in cmd_text.lower():       # show run | include snmp-server location
        return 'location'
    cmd_text = cmd_text.split('|')[0].strip().lower()
    words = cmd_text.split()
    if not words:
        return None
    for key, canon in COMMANDS:
        if len(words) < len(canon):
            continue
        if len(words) > len(canon) and key != 'inventory':        # 'show inventory all'
            continue
        ok = True
        for typed, full in zip(words, canon):
            if not (full.startswith(typed) or typed.startswith(full)):
                ok = False
                break
            if typed != full and len(typed) < 2:
                ok = False
                break
        if ok:
            return key
    return 'unknown'


def hostname_from_version(text):
    for rx in _HOST_FROM_VERSION:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


def name_from_path(path, root):
    """Guess a device name (or IP) from a file name, then its folder name."""
    candidates = [(os.path.splitext(os.path.basename(path))[0], 'file name')]
    parent = os.path.dirname(path)
    if os.path.normcase(os.path.abspath(parent)) != os.path.normcase(os.path.abspath(root)):
        candidates.append((os.path.basename(parent), 'folder name'))
    for raw, source in candidates:
        for tok in re.split(r'[_\s]+', raw):
            if is_ipv4(tok):
                return tok, source
            low = tok.lower()
            if (low in _NAME_NOISE or not re.search(r'[A-Za-z]', tok) or len(tok) < 2
                    or re.match(r'^\d{4}-\d\d-\d\d', tok) or re.match(r'^\d{6,}$', tok)):
                continue
            return tok, source
    return None, None


def _segment_by_prompt(lines):
    """Yield (host, cmd_text, start, end) for each prompt echo."""
    marks = []
    for i, line in enumerate(lines):
        m = PROMPT_RE.match(line)
        if m and (not m.group('cmd') or re.match(r'^(sh|show|term|terminal|en|enable|exit|conf)\b',
                                                    m.group('cmd').strip(), re.I)):
            marks.append((i, m.group('host'), m.group('cmd').strip()))
    out = []
    for n, (i, host, cmd) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        out.append((host, cmd, i + 1, end))
    return out


def _segment_by_signature(lines):
    """Return [(cmd, start, end)] using output signatures. Lines before the first are dropped."""
    starts = []
    current = None
    prev_nonblank = ''
    for i, line in enumerate(lines):
        for cmd, rx, strong in SIGNATURES:
            if not rx.match(line):
                continue
            if cmd == current:
                break
            if cmd == 'version' and current == 'neighbors':
                # A neighbour's version banner, not this device's 'show version'.
                if not strong or _NEIGHBOR_VERSION_LEAD.match(prev_nonblank):
                    break
            starts.append((cmd, i))
            current = cmd
            break
        if line.strip():
            prev_nonblank = line
    out = []
    for n, (cmd, i) in enumerate(starts):
        end = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
        out.append((cmd, i, end))
    return out


# SolarWinds NCM "Execute Command Script" results: blocks between lines of underscores, each
# headed 'NODE-CAPTION (IP):' (IPv4 or IPv6); commands are echoed as bare lines, no prompt.
# The caption is the SolarWinds node name, which may carry a note after the hostname when the
# hostname is ambiguous: 'campus-as1 (BLDG 2) (10.1.1.2):'.
_NCM_RULE = re.compile(r'^_{20,}\s*$')
_NCM_NODE = re.compile(r'^(?P<host>[A-Za-z0-9].*?)\s+\((?P<ip>[0-9A-Fa-f:.]*\d[0-9A-Fa-f:.]*)\):\s*$')
_NCM_ERROR = re.compile(r'^\s*ERROR:\s*(.+?)\s*$')
# A command echo: bare ('show inventory') or after a prompt ('RTR1#show inventory').
_BARE_COMMAND = re.compile(r'^(?:[A-Za-z0-9][\w.\-]*(?:\([\w\-]+\))?[#>]\s*)?(?:sh|show)\s+\S')
_PROMPT_PREFIX = re.compile(r'^[A-Za-z0-9][\w.\-]*(?:\([\w\-]+\))?[#>]\s*')
# Outputs recognised by their content inside another section (a lost or mangled echo).
_RESCUE = ('version', 'inventory', 'module', 'fex', 'vdc', 'neighbors')


_OWN_VERSION = re.compile(r'^\S+\s+uptime is\s|^\s*Device name:', re.M)


def _by_content(cmd, lines):
    """[(cmd, start, end, rescued)]: the section split wherever another command's output starts
    (its signature line), so output whose echo was lost - e.g. glued to a '!' or missing - is not
    swallowed by the command before it. A 'show version' is only cut out when it is a device's
    own (it has 'uptime is' / 'Device name:'), never a neighbour's banner. rescued = recognised by
    content, not by its echo: such output loses to an echoed copy of the same command."""
    segs = _segment_by_signature(lines)
    out, current, start, rescued = [], cmd, 0, False
    for n, (sig, s, e) in enumerate(segs):
        if s == 0 or sig == current or sig not in _RESCUE:
            continue
        if sig == 'version' and not _OWN_VERSION.search('\n'.join(lines[s:e])):
            continue
        out.append((current, start, s, rescued))
        current, start, rescued = sig, s, True
    out.append((current, start, len(lines), rescued))
    return out


def _echo(line, host):
    """The command text when a line is a command echo: bare ('show inventory'), or after this
    node's own prompt ('RTR1#show inventory'). A prompt naming another device - e.g. text in a
    banner - is not an echo."""
    if not _BARE_COMMAND.match(line):
        return None
    m = _PROMPT_PREFIX.match(line)
    if m:
        prompt = re.split(r'[#>(]', m.group(0))[0]
        if norm_host(prompt) != norm_host(host or ''):
            return None
        return line[m.end():]
    return line


def _split_ncm(path, lines):
    """Sections from NCM script output, or None if the file is not in that layout."""
    blocks, cur = [], None
    for i, line in enumerate(lines):
        if _NCM_RULE.match(line):
            cur = None
            continue
        m = _NCM_NODE.match(line)
        if m and cur is None and ('.' in m.group('ip') or ':' in m.group('ip')):
            host, note = split_caption(m.group('host'))
            cur = {'host': host, 'note': note, 'ip': m.group('ip'), 'lines': []}
            blocks.append(cur)
        elif cur is not None:
            cur['lines'].append(line)
    if not blocks:
        return None
    sections = []
    for bn, b in enumerate(blocks):
        body = b['lines']
        err = [m.group(1) for m in (_NCM_ERROR.match(l) for l in body) if m]
        if err and not any(_BARE_COMMAND.match(l) for l in body):
            sections.append(Section(path, 'error', err[0], 'ncm', host=b['host'],
                                    host_source='SolarWinds header', ip_hint=b['ip'], note=b['note'],
                                    block=(path, bn)))
            continue
        block = (path, bn)
        marks = []
        for i, l in enumerate(body):
            text = _echo(l, b['host'])
            if text is not None:
                marks.append((i, match_command(text)))
        # Output before the first echo (or all of it, when nothing was echoed): by its content.
        head = body[:marks[0][0]] if marks else body
        for cmd, start, end in _segment_by_signature(head):
            sections.append(Section(path, cmd, '\n'.join(head[start:end]), 'ncm+signature',
                                    host=b['host'], host_source='SolarWinds header',
                                    ip_hint=b['ip'], note=b['note'], block=block))
        for n, (i, key) in enumerate(marks):
            end = marks[n + 1][0] if n + 1 < len(marks) else len(body)
            part = body[i + 1:end]
            for cmd, s, e, rescued in _by_content(key, part):
                sections.append(Section(path, cmd, '\n'.join(part[s:e]), 'ncm+content' if rescued else 'ncm',
                                        host=b['host'], host_source='SolarWinds header',
                                        ip_hint=b['ip'], note=b['note'], block=block))
    return sections


def split_file(path, root):
    """Return (sections, file_stats) for one file."""
    stats = {'layout': None, 'devices_in_file': 0}
    text = read_text(path)
    lines = text.split('\n')
    sections = []

    ncm = _split_ncm(path, lines)
    if ncm is not None:
        stats['layout'] = 'SolarWinds NCM script results'
        stats['devices_in_file'] = len({(s.host, s.note) for s in ncm})
        return ncm, stats

    prompt_segments = _segment_by_prompt(lines)
    if prompt_segments:
        stats['layout'] = 'prompt'
        hosts = set()
        for host, cmd_text, start, end in prompt_segments:
            key = match_command(cmd_text) if cmd_text else None
            part = lines[start:end]
            if key is None:                 # a bare prompt: keep only output that follows it
                if not any(l.strip() for l in part):
                    continue
                key = 'unknown'
            hosts.add(host.lower())         # a command with no output still names its device
            for cmd, s, e, rescued in _by_content(key, part):
                sections.append(Section(path, cmd, '\n'.join(part[s:e]), 'prompt+content' if rescued else 'prompt',
                                        host=host, host_source='prompt', block=(path, host.lower())))
        stats['devices_in_file'] = len(hosts)
        return sections, stats

    segments = _segment_by_signature(lines)
    if not segments:
        stats['layout'] = 'unrecognised'
        return sections, stats

    # Prompt-less file. Each 'show version' starts a new device if there are several.
    version_count = sum(1 for cmd, _, _ in segments if cmd == 'version')
    stats['layout'] = 'multi-device (no prompts)' if version_count > 1 else 'single device (no prompts)'
    stats['devices_in_file'] = max(1, version_count)

    file_name, file_source = name_from_path(path, root)
    ip_hint = file_name if file_name and is_ipv4(file_name) else None
    preamble_ip = IPV4_RE.search('\n'.join(lines[:segments[0][1]]))
    if not ip_hint and preamble_ip and is_ipv4(preamble_ip.group(0)):
        ip_hint = preamble_ip.group(0)

    group = []
    groups = [group]
    for cmd, start, end in segments:
        if cmd == 'version' and version_count > 1 and group:
            group = []
            groups.append(group)
        group.append(Section(path, cmd, '\n'.join(lines[start:end]), 'signature', ip_hint=ip_hint))

    for grp in groups:
        host, source = None, None
        for sec in grp:
            if sec.cmd == 'version':
                host = hostname_from_version(sec.text)
                source = 'show version' if host else None
                break
        if not host and version_count <= 1 and file_name:
            host, source = file_name, file_source
        for sec in grp:
            sec.host, sec.host_source = host, source
        sections.extend(grp)
    return sections, stats


def iter_input_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            yield os.path.join(dirpath, fn)


def split_inputs(root, progress=None):
    """Split every text file under `root`. Returns (sections, stats)."""
    stats = {'files_seen': 0, 'files_used': 0, 'files_skipped': 0, 'bytes': 0,
             'layouts': {}, 'skipped_reasons': {}}
    all_sections = []
    base = root if os.path.isdir(root) else os.path.dirname(root)
    for path in iter_input_files(root):
        stats['files_seen'] += 1
        ext = os.path.splitext(path)[1].lower()
        reason = None
        try:
            size = os.path.getsize(path)
            if ext in SKIP_EXTS:
                reason = 'not a text export (%s)' % ext
            elif size == 0:
                reason = 'empty file'
            elif size > MAX_FILE_BYTES:
                reason = 'larger than 50 MB'
            elif looks_binary(path):
                reason = 'binary content'
        except OSError as exc:
            reason = 'unreadable (%s)' % exc.__class__.__name__
        if reason:
            stats['files_skipped'] += 1
            stats['skipped_reasons'][reason] = stats['skipped_reasons'].get(reason, 0) + 1
            continue
        stats['bytes'] += size
        sections, fstats = split_file(path, base)
        layout = fstats['layout']
        stats['layouts'][layout] = stats['layouts'].get(layout, 0) + 1
        if sections:
            stats['files_used'] += 1
        all_sections.extend(sections)
        if progress:
            progress(stats['files_seen'], path)

    _inherit_folder_hosts(all_sections)
    return all_sections, stats


def _inherit_folder_hosts(sections):
    """Prompt-less files with no usable name inherit the one hostname found in their folder
    (NCM config-archive style: one folder per device, one file per command)."""
    by_folder = {}
    for sec in sections:
        if sec.how == 'signature' and sec.host_source == 'show version':
            by_folder.setdefault(os.path.dirname(sec.path), set()).add(sec.host)
    for sec in sections:
        if sec.how != 'signature' or sec.host_source == 'show version':
            continue
        hosts = by_folder.get(os.path.dirname(sec.path), set())
        if len(hosts) == 1 and sec.host_source != 'file name':
            sec.host, sec.host_source = next(iter(hosts)), 'folder (show version in same folder)'
