"""Workspace: one folder per network holding parsed snapshots (plain JSON).

    <workspace>/networks/<NETWORK>/snapshot_<YYYYMMDD-HHMMSS>.json

A snapshot is a network's whole inventory after one import: devices and hardware from
SolarWinds (devices it could not reach this time carried forward from earlier imports), the
devices on the network's manual inventory sheet, per-part history and parse statistics.
Importing SolarWinds output keeps the manual devices; importing the manual sheet keeps the
SolarWinds devices. It is also the transfer file: copy it from a network's PC to the central
network and use "Import snapshot file". The newest snapshot of each network is the current
state; older ones are kept so changes between imports can be listed.

    <workspace>/settings.json    {"keep_unreached_days": null, "include_optics": false,
                                  "network_order": [...], "checked_networks": [...],
                                  "exclude": {"NETWORK": ["PS-*", ...]},
                                  "shared_serials": ["FOC00000000X", ...],
                                  "kind_rules": [["power", "pid", "^C9350-PWR"], ...],
                                  "role_rules": [["access", "^C9350"], ...]}

Exclusion rules hide devices (e.g. another team's equipment on a shared SolarWinds) from
everything shown and written. They are applied when a snapshot is loaded, so the devices are
still imported and tracked, and removing a rule brings them back without a re-import.
Shared serials (factory default serials reported by different modules) are applied the same way.

The inventory is taken about once a year, often as a first SolarWinds job plus retries for the
devices that failed. Changes therefore compare with the previous round - the newest snapshot
at least ROUND_GAP_DAYS older than the current one - rather than with the retry before it.
"""

import datetime
import hashlib
import json
import os
import re
import shutil

from . import VERSION
from .splitter import split_inputs
from .model import (apply_kind_rules, build_devices, display_name, drop_unserialized, excluded_by,
                    hide_optics, include_rules, load_node_list, mark_shared_serials)
from . import kinds, roles
from .history import DEFAULT_KEEP_DAYS, ROUND_GAP_DAYS, annotate, carry_forward, merge_manual, update_history
from .manual import manual_stats, read_manual

_SETS = ('commands', 'host_sources', 'files')
_STAMP = re.compile(r'snapshot_(\d{8})-(\d{6})\.json$')


_KEY_SEP = ' || '


def _jsonable(obj):
    if isinstance(obj, dict):       # tuple keys (part number, description) -> 'pid || descr'
        return {(_KEY_SEP.join(k) if isinstance(k, tuple) else k): _jsonable(v)
                for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(_jsonable(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _restore(snap):
    for d in snap['devices']:
        for k in _SETS:
            if k in d:
                d[k] = set(d[k])
    for k in ('links', 'external'):     # mapping data in snapshots from earlier versions
        snap.pop(k, None)
    bs = snap.get('build_stats') or {}
    bs['unrecognised'] = {tuple(k.split(_KEY_SEP, 1)): v for k, v in bs.get('unrecognised', {}).items()}
    return snap


def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name.strip()) or 'NETWORK'


class Workspace(object):
    def __init__(self, root):
        self.root = root
        self.net_dir = os.path.join(root, 'networks')
        if not os.path.isdir(self.net_dir):
            os.makedirs(self.net_dir)

    # ------------------------------------------------------------------ networks
    def networks(self):
        """Networks in the user's order (network_order setting), new ones at the end."""
        found = sorted(n for n in os.listdir(self.net_dir)
                       if os.path.isdir(os.path.join(self.net_dir, n)) and self.snapshots(n))
        order = [n for n in self.settings().get('network_order') or [] if n in found]
        return order + [n for n in found if n not in order]

    def set_network_order(self, names):
        self._update_settings(network_order=list(names))

    def checked_networks(self):
        """Networks ticked in the app (all, until the user changes it)."""
        nets = self.networks()
        saved = self.settings().get('checked_networks')
        if saved is None:
            return nets
        known = set(self.settings().get('known_networks') or saved)
        return [n for n in nets if n in saved or n not in known]      # new networks start ticked

    def set_checked_networks(self, names):
        self._update_settings(checked_networks=list(names), known_networks=self.networks())

    def exclusions(self, network):
        return list((self.settings().get('exclude') or {}).get(network) or [])

    def include(self, network, devices):
        """Bring excluded devices back (see model.include_rules)."""
        rules = self.exclusions(network)
        for d in devices:
            rules = include_rules(d, rules)
        self.set_exclusions(network, rules)

    def set_exclusions(self, network, patterns):
        rules = dict(self.settings().get('exclude') or {})
        clean = []
        for p in patterns:
            if p.strip() and p.strip() not in clean:
                clean.append(p.strip())
        if clean:
            rules[network] = clean
        else:
            rules.pop(network, None)
        self._update_settings(exclude=rules)

    def use_rules(self):
        """Load the settings' kind_rules / role_rules. Returns the problems (bad entries)."""
        s = self.settings()
        self.rule_problems = (kinds.set_extra_rules(s.get('kind_rules'))
                              + roles.set_extra_rules(s.get('role_rules')))
        return self.rule_problems

    def set_rules(self, kind_rules, role_rules):
        def plain(rules):
            return [list(r) if isinstance(r, tuple) else r for r in rules]
        self._update_settings(kind_rules=plain(kind_rules), role_rules=plain(role_rules))

    def shared_serials(self):
        """Serials acknowledged as shared by different parts (a factory default serial)."""
        return list(self.settings().get('shared_serials') or [])

    def set_shared_serials(self, serials):
        clean = []
        for s in serials:
            if s.strip() and s.strip() not in clean:
                clean.append(s.strip())
        self._update_settings(shared_serials=sorted(clean))

    def snapshots(self, network):
        folder = os.path.join(self.net_dir, safe_name(network))
        if not os.path.isdir(folder):
            return []
        return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                      if f.startswith('snapshot_') and f.endswith('.json'))

    def load(self, network, index=-1, optics=None, raw=False):
        """Load a snapshot. Parts without a serial number are dropped again, so a snapshot
        made by an earlier version follows the same rule. Optics are hidden unless optics=True
        (default: the include_optics setting), and devices matching the network's exclusion
        rules move to snap['excluded']. raw=True: everything, as stored (for imports)."""
        snaps = self.snapshots(network)
        if not snaps or abs(index) > len(snaps):
            return None
        with open(snaps[index], encoding='utf-8') as fh:
            snap = _restore(json.load(fh))
        for d in snap['devices']:
            if d['hardware']:
                drop_unserialized(d)
        annotate(snap['devices'], snap.get('history'))
        return snap if raw else self._view(network, snap, optics)

    def _view(self, network, snap, optics=None):
        self.use_rules()
        apply_kind_rules(snap['devices'])
        if not (self.include_optics() if optics is None else optics):
            hide_optics(snap['devices'])
        mark_shared_serials(snap['devices'], self.shared_serials())
        rules = self.exclusions(network)
        keep, out = [], []
        for d in snap['devices']:
            rule = excluded_by(d, rules)
            if rule:
                d['excluded_by'] = rule
                out.append(d)
            else:
                keep.append(d)
        snap['devices'], snap['excluded'] = keep, out
        return snap

    def snapshot_times(self, network):
        """Import time of each snapshot (from the file names), oldest first."""
        out = []
        for path in self.snapshots(network):
            m = _STAMP.search(path)
            out.append(datetime.datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S') if m else None)
        return out

    def baseline_index(self, network, mode='round'):
        """Snapshot index to compare the current one with, or None for a first import.
        mode 'round': the newest snapshot at least ROUND_GAP_DAYS older than the current one
        (last year's inventory, not this year's retry job); falls back to the previous import.
        mode 'previous': the import before the current one."""
        times = self.snapshot_times(network)
        if len(times) < 2:
            return None
        if mode == 'round' and times[-1]:
            cut = times[-1] - datetime.timedelta(days=ROUND_GAP_DAYS)
            older = [i for i, t in enumerate(times[:-1]) if t and t <= cut]
            if older:
                return older[-1] - len(times)
        return -2

    def forget(self, network, names):
        """Remove devices that are gone for good (e.g. decommissioned, still carried as 'not
        reached'). Saves a new snapshot without them; Changes lists them as missing. Manual
        devices are not touched - edit the manual sheet for those. names: hostnames, shown names
        ('campus-as1 (BLDG 2)') or device keys. Returns the names removed."""
        prev = self.load(network, raw=True)
        want = set(n.strip().lower() for n in names)
        drop = [d for d in prev['devices'] if d.get('source', 'SolarWinds') != 'manual' and
                want & {d['key'], d['hostname'].lower(), display_name(d).lower()}]
        gone = [display_name(d) for d in drop]
        if gone:
            created = max(_now(), (datetime.datetime.strptime(prev['created'], '%Y-%m-%d %H:%M:%S')
                                   + datetime.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'))
            snap = dict(prev, created=created, source='forgotten: ' + ', '.join(gone),
                        devices=[d for d in prev['devices'] if d not in drop])
            snap['import'] = 'forget'
            self._store(network, snap)
        return gone

    # ------------------------------------------------------------------ settings
    def settings(self):
        try:
            with open(os.path.join(self.root, 'settings.json'), encoding='utf-8') as fh:
                s = json.load(fh)
            return s if isinstance(s, dict) else {}     # a file that is not {...}: as if empty
        except (OSError, ValueError):
            return {}

    def _update_settings(self, **values):
        settings = self.settings()
        settings.update(values)
        self.save_settings(settings)

    def save_settings(self, settings):
        with open(os.path.join(self.root, 'settings.json'), 'w', encoding='utf-8') as fh:
            json.dump(settings, fh, indent=2)

    def keep_days(self):
        """How long a device SolarWinds cannot reach keeps its last known hardware.
        None (the default) = until it is forgotten."""
        v = self.settings().get('keep_unreached_days', DEFAULT_KEEP_DAYS)
        return None if v is None or v == '' else int(v)

    def include_optics(self):
        """List and count optics (SFPs ...)? Off by default."""
        return bool(self.settings().get('include_optics', False))

    def remove(self, network):
        shutil.rmtree(os.path.join(self.net_dir, safe_name(network)), ignore_errors=True)
        s = self.settings()
        for key in ('network_order', 'checked_networks', 'known_networks'):
            if network in (s.get(key) or []):
                s[key] = [n for n in s[key] if n != network]
        (s.get('exclude') or {}).pop(network, None)
        self.save_settings(s)

    # ------------------------------------------------------------------ import
    def import_exports(self, network, input_path, nodes_csv=None, progress=None, created=None):
        """Parse SolarWinds exports and save a snapshot. Devices not reached this time keep
        their last known hardware (see history.py); the manual sheet's devices are kept.
        Returns the snapshot dict."""
        sections, split_stats = split_inputs(input_path, progress=progress)
        nodes, nodes_info = (load_node_list(nodes_csv) if nodes_csv else ({}, None))
        self.use_rules()
        fresh, build_stats = build_devices(sections, nodes)
        created = created or _now()
        prev = self.load(network, raw=True)
        devices = carry_forward(prev, fresh, created, self.keep_days(), self.shared_serials())
        devices = merge_manual(devices, [d for d in (prev or {}).get('devices', [])
                                         if d.get('source') == 'manual'])
        snap = {'format': 'netinv-snapshot-1', 'tool_version': VERSION, 'network': network,
                'created': created, 'import': 'SolarWinds',
                'source': os.path.abspath(input_path), 'nodes_csv': nodes_csv or '',
                'devices': devices, 'split_stats': split_stats, 'build_stats': build_stats, 'nodes_info': nodes_info}
        snap['history'] = update_history(prev, devices, 'SolarWinds', created)
        return self._store(network, snap)

    def import_manual(self, path, default_network='', created=None):
        """Read a manual inventory sheet. Each network named on it (blank = default_network)
        gets a new snapshot whose manual devices are exactly the sheet's rows; its SolarWinds
        devices are kept. Returns ({network: snapshot}, info)."""
        by_net, info = read_manual(path, default_network)
        created = created or _now()
        out = {}
        for network, manual in sorted(by_net.items()):
            network = safe_name(network)
            prev = self.load(network, raw=True)
            keep = [d for d in (prev or {}).get('devices', []) if d.get('source', 'SolarWinds') != 'manual']
            for d in keep:                       # this import's manual rows are checked afresh
                d['warnings'] = [w for w in d['warnings'] if not w.startswith('Also on the manual')]
            devices = merge_manual(keep, manual)
            build_stats, split_stats = manual_stats(info)
            snap = {'format': 'netinv-snapshot-1', 'tool_version': VERSION, 'network': network,
                    'created': created, 'import': 'manual', 'source': os.path.abspath(path),
                    'nodes_csv': '', 'devices': devices, 'split_stats': split_stats,
                    'build_stats': build_stats, 'nodes_info': None}
            snap['history'] = update_history(prev, devices, 'manual', created)
            out[network] = self._store(network, snap)
        return out, info

    def _store(self, network, snap):
        self._save(network, snap, stamp=re.sub(r'\D', '', snap['created'])[:14])
        snap = _restore(json.loads(json.dumps(_jsonable(snap), default=str)))
        annotate(snap['devices'], snap['history'])
        return self._view(network, snap)

    def import_snapshot_file(self, path):
        """Add a snapshot made on another PC (the transfer file). Returns the network name."""
        with open(path, encoding='utf-8') as fh:
            snap = json.load(fh)
        if snap.get('format') != 'netinv-snapshot-1':
            raise ValueError('%s is not a NetInv snapshot' % os.path.basename(path))
        self._save(snap['network'], snap, stamp=re.sub(r'\D', '', snap.get('created', ''))[:14])
        return snap['network']

    def _save(self, network, snap, stamp=None):
        folder = os.path.join(self.net_dir, safe_name(network))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        stamp = stamp or datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        path = os.path.join(folder, 'snapshot_%s-%s.json' % (stamp[:8], stamp[8:14]))
        while os.path.exists(path):          # two imports in the same second
            stamp = '%014d' % (int(stamp[:14]) + 1)
            path = os.path.join(folder, 'snapshot_%s-%s.json' % (stamp[:8], stamp[8:14]))
        body = json.dumps(_jsonable(snap), indent=1, default=str, sort_keys=True)
        snap_sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(body)
        with open(path + '.sha256', 'w') as fh:
            fh.write('%s  %s\n' % (snap_sha, os.path.basename(path)))
        return path


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------- changes

def diff_snapshots(old, new):
    """What changed between two snapshots of one network: [(change, device name, detail)].
    Devices are matched by key (hostname + SolarWinds note), not by the shown name."""
    if not old:
        return []
    before = {d['key']: d for d in old['devices']}
    after = {d['key']: d for d in new['devices']}
    out = []
    for k in sorted(set(after) - set(before), key=lambda k: display_name(after[k])):
        out.append(('New device', display_name(after[k]), after[k]['model']))
    for k in sorted(set(before) - set(after), key=lambda k: display_name(before[k])):
        out.append(('Missing device', display_name(before[k]), before[k]['model']))
    for k in sorted(set(before) & set(after), key=lambda k: display_name(after[k])):
        a, b = before[k], after[k]
        h = display_name(b)
        if b.get('stale') and not a.get('stale'):
            out.append(('Not reached', h, 'SolarWinds could not reach it - hardware as last seen %s'
                        % b.get('last_seen')))
        elif a.get('stale') and not b.get('stale'):
            out.append(('Reached again', h, 'last reached %s' % a.get('last_seen')))
        if a['version'] != b['version'] and a['version'] and b['version']:
            out.append(('Software changed', h, '%s -> %s' % (a['version'], b['version'])))
        if b.get('source') == 'manual' or a.get('source') == 'manual':
            out.extend(_diff_by_serial(h, a, b))
            continue
        # built-in parts go with the chassis (and older snapshots did not list them)
        old_parts = {(p['kind'], p['name']): p['serial'] for p in a['hardware'] if p['kind'] != 'builtin'}
        new_parts = {(p['kind'], p['name']): p['serial'] for p in b['hardware'] if p['kind'] != 'builtin'}
        # a serial still there under another kind or name (a newer version classifies it
        # differently) is neither added nor removed
        old_all = set(p['serial'] for p in a['hardware'])
        new_all = set(p['serial'] for p in b['hardware'])
        for key in sorted(set(old_parts) | set(new_parts)):
            s1, s2 = old_parts.get(key), new_parts.get(key)
            if s1 == s2:
                continue
            if s1 and s2:
                out.append(('Hardware swapped', h, '%s: %s -> %s' % (key[1], s1, s2)))
            elif s2 is not None and s1 is None and s2 not in old_all:
                out.append(('Hardware added', h, '%s %s' % (key[1], s2)))
            elif s1 is not None and s2 is None and s1 not in new_all:
                out.append(('Hardware removed', h, '%s %s' % (key[1], s1)))
    return out


def _diff_by_serial(h, a, b):
    """Manual devices (and stock shelves holding many of one part): compare serial numbers."""
    old = dict((p['serial'], p) for p in a['hardware'])
    new = dict((p['serial'], p) for p in b['hardware'])
    out = []
    for sn in sorted(set(new) - set(old)):
        out.append(('Hardware added', h, '%s %s %s' % (new[sn]['name'], new[sn]['pid'], sn)))
    for sn in sorted(set(old) - set(new)):
        out.append(('Hardware removed', h, '%s %s %s' % (old[sn]['name'], old[sn]['pid'], sn)))
    for sn in sorted(set(old) & set(new)):
        if (old[sn]['role'] or '') != (new[sn]['role'] or ''):
            out.append(('Status changed', h, '%s %s: %s -> %s' % (new[sn]['pid'], sn, old[sn]['role'],
                                                                   new[sn]['role'])))
    return out
