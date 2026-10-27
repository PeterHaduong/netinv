"""Small shared helpers: file reading, hostname normalisation, serial numbers."""

import codecs
import re

IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

# What devices print instead of a serial number (empty slots, provisioned-but-absent stack
# members, fans without a serial EEPROM).
_NO_SERIAL = {'', 'N/A', 'NA', 'NONE', 'NULL', 'UNKNOWN', 'NOT AVAILABLE', 'NOT SET', '-', '--'}


def read_text(path):
    """Read a text file whatever its encoding (UTF-8, UTF-8 BOM, UTF-16 from Windows tools)."""
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        text = raw.decode('utf-16', errors='replace')
    elif raw.startswith(codecs.BOM_UTF8):
        text = raw[3:].decode('utf-8', errors='replace')
    else:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('cp1252', errors='replace')
    # Normalise line endings and strip terminal backspace/bell noise.
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)


def looks_binary(path, sample=4096):
    with open(path, 'rb') as fh:
        chunk = fh.read(sample)
    if chunk.startswith(codecs.BOM_UTF16_LE) or chunk.startswith(codecs.BOM_UTF16_BE):
        return False
    return b'\x00' in chunk


def is_ipv4(s):
    if not s or not IPV4_RE.fullmatch(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


def norm_host(name):
    """Comparable form of a hostname: 'Core-1.example.com' / 'CORE-1(FOX123)' /
    'core-1 (BLDG 2)' -> 'core-1'."""
    if not name:
        return ''
    name = name.strip().strip('"')
    name = re.sub(r'\s*\(.*\)$', '', name).strip()
    if not is_ip(name):
        name = name.split('.')[0]
    return name.lower()


def short_host(name):
    """'as1.example.com' -> 'as1' (case kept). IP addresses are left alone."""
    name = (name or '').strip()
    return name if is_ip(name) else name.split('.')[0]


_IPV6 = re.compile(r'^[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:\d{1,3}(?:\.\d{1,3}){3})?(?:%[\w/.:-]+)?(?=\s|$|[(\[{<"-])')
_CAPTION_REST = re.compile(r'^\s*(.*?)\s*$')


def _ipv6_prefix(text):
    """The IPv6 address at the start of text (match object) or None. 'DC1:A:B' is not one:
    an address has '::' or all eight groups (seven colons; six with a dotted IPv4 tail)."""
    m = _IPV6.match(text)
    if not m:
        return None
    addr = m.group(0).split('%')[0]
    if '::' in addr or addr.count(':') >= (6 if '.' in addr else 7):
        return m
    return None


def is_ip(text):
    """An IPv4 or IPv6 address (the whole text)."""
    text = (text or '').strip()
    m = _ipv6_prefix(text)
    return is_ipv4(text) or bool(m and m.end() == len(text))
_CAPTION = re.compile(r'^\s*([A-Za-z0-9][\w.\-]*)\s*(.*?)\s*$')


def split_caption(caption):
    """A SolarWinds node caption -> (hostname, note). Ambiguous hostnames get a note in
    SolarWinds: 'campus-as1 (BLDG 2)', 'campus-as1 [BLDG 2]', 'campus-as1 - BLDG 2' ->
    ('campus-as1', 'BLDG 2'). 'campus-as1' -> ('campus-as1', '')."""
    v6 = _ipv6_prefix((caption or '').strip())       # a node captioned by its IPv6 address
    m = _CAPTION_REST.match(caption.strip()[v6.end():]) if v6 else _CAPTION.match(caption or '')
    if v6:
        return v6.group(0), _note(m.group(1))
    m = _CAPTION.match(caption or '')
    if not m:
        return (caption or '').strip(), ''
    return m.group(1), _note(m.group(2))


def _note(note):
    while note and (note[0] + note[-1] in ('()', '[]', '{}', '<>', '""') or note[0] in '-:/|,'):
        note = (note[1:-1] if note[0] + note[-1] in ('()', '[]', '{}', '<>', '""') else note[1:]).strip()
    return note


def device_key(hostname, note=''):
    """Identity of a device across imports: the hostname, plus the SolarWinds note when there is
    one, so two nodes that are both really 'campus-as1' stay two devices."""
    key = norm_host(hostname)
    return '%s | %s' % (key, note.strip().lower()) if note and note.strip() else key


def real_serial(sn):
    """The serial number, or '' when the device printed a placeholder ('N/A', '0', '0000')."""
    sn = (sn or '').strip()
    if sn.upper() in _NO_SERIAL or not sn.strip('0'):
        return ''
    return sn
