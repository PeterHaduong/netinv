"""What was seen when: keeping unreached devices, and first / last seen per part.

SolarWinds does not reach every device every run. A device that was collected before but not
this time (connection refused, or missing from the results) keeps its last known hardware,
marked "not reached since <date>". By default it stays until someone removes it ("Forget
device" - the inventory runs about once a year, so a year-old record is normal); a
keep_unreached_days workspace setting can drop such devices automatically instead.

Each snapshot carries a history of every serial number ever seen in that network:
first / last seen, in how many imports it was seen out of the imports that reached its
device ('checked'), and how many times it came back after being absent ('gaps' - inventory
that comes and goes). A device that was not reached - e.g. absent from a retry job that only
covered the failed devices - does not make its parts count as absent. Parts from the manual
sheet are tracked the same way.
"""

import copy
import datetime

from .model import in_inventory
from .textutil import norm_host

DEFAULT_KEEP_DAYS = None                 # None = keep unreached devices until forgotten
# Imports less than this many days apart are one inventory round (a job plus its retries).
ROUND_GAP_DAYS = 30


def _day(stamp):
    return (stamp or '')[:10]


def reached(d):
    """Collected this time: not a connection error, and something was read."""
    return 'collection error' not in d['commands'] and bool(d['hardware'] or d['serial'] or d.get('vdc_of'))


def carry_forward(prev, fresh, created, keep_days=DEFAULT_KEEP_DAYS):
    """fresh: SolarWinds devices from this import. Returns the device list with devices that
    were not reached replaced by (or added as) their last known record. created: this import's
    timestamp 'YYYY-MM-DD HH:MM:SS'. keep_days None = keep forever, 0 = never keep.

    A device collected earlier in this round (within ROUND_GAP_DAYS, e.g. by the first job when
    this import is a retry of the failures) is carried as current ('carried'), not as unreached."""
    today = _day(created)
    for d in fresh:
        d['carried'] = False
        if reached(d):
            d['last_seen'] = today
    if not prev:
        return list(fresh)
    by_key = dict((d['key'], d) for d in fresh)
    owned = {}                                   # serial -> fresh device that has it
    for d in fresh:
        if reached(d):
            for sn in (d['serial'] or '').split(' / '):
                if sn:
                    owned[sn] = d
    now = datetime.datetime.strptime(today, '%Y-%m-%d')
    out = [d for d in fresh]
    for p in prev['devices']:
        if p.get('source', 'SolarWinds') != 'SolarWinds' or not in_inventory(p):
            continue
        cur = by_key.get(p['key'])
        if cur is not None and reached(cur):
            continue
        last = p.get('last_seen') or _day(prev.get('created'))
        try:
            age = (now - datetime.datetime.strptime(last, '%Y-%m-%d')).days
        except ValueError:
            age = 0
        holder = owned.get(p['serial'].split(' / ')[0]) if p['serial'] else None
        if holder is not None and (norm_host(holder['hostname']) == norm_host(p['hostname'])
                                   or p.get('platform') != 'NX-OS'):
            continue    # the same box, collected now under another name (e.g. another SolarWinds)
        if not p.get('stale') and age <= ROUND_GAP_DAYS:      # collected earlier this round
            old = copy.deepcopy(p)
            old['carried'] = True
            if cur is not None:
                out.remove(cur)
            if holder is not None:
                old.update(hardware=[], members=[], vdc_of=holder['hostname'])
            out.append(old)
            continue
        if keep_days is not None and age > keep_days:
            continue
        old = copy.deepcopy(p)
        old.update(stale=True, carried=False, last_seen=last, commands=set(p['commands']) | {'not reached'})
        old['warnings'] = ['Not reached by SolarWinds this import - showing hardware last seen %s' % last]
        if cur is not None:                      # still listed, but could not be collected
            old['warnings'] += cur['warnings']
            old['mgmt_ip'] = cur['mgmt_ip'] or old['mgmt_ip']
            out.remove(cur)
        if holder is not None:                   # e.g. the other VDC of this chassis answered
            old.update(hardware=[], members=[], vdc_of=holder['hostname'])
        out.append(old)
    return out


def merge_manual(solarwinds, manual):
    """One device list: SolarWinds devices plus the manual sheet's. A hostname in both keeps the
    SolarWinds record (it is read from the device itself)."""
    names = dict((d['key'], d) for d in solarwinds)
    out = list(solarwinds)
    for m in manual:
        sw = names.get(m['key'])
        if sw is not None:
            sw['warnings'].append('Also on the manual inventory sheet - the SolarWinds data is used')
            continue
        out.append(m)
    return out


def _bootstrap(prev):
    """History for a snapshot made before history existed: everything seen once, then."""
    hist = {'imports': {}, 'parts': {}}
    if not prev:
        return hist
    day = _day(prev.get('created'))
    for d in prev['devices']:
        src = d.get('source', 'SolarWinds')
        hist['imports'][src] = 1
        for it in d['hardware']:
            if it.get('serial'):
                hist['parts']['%s|%s' % (src, it['serial'])] = {
                    'first_seen': day, 'last_seen': day, 'seen': 1, 'checked': 1, 'gaps': 0,
                    'in_last': True, 'pid': it['pid'], 'host': d['key']}
    return hist


def update_history(prev, devices, source, created):
    """Record this import (source 'SolarWinds' or 'manual') and annotate every part in devices
    with first_seen / last_seen / seen / of / gaps. Returns the history to store."""
    hist = copy.deepcopy(prev['history']) if prev and prev.get('history') else _bootstrap(prev)
    day = _day(created)
    n = hist['imports'].get(source, 0) + 1
    hist['imports'][source] = n
    now, reached_keys = {}, set()
    for d in devices:
        if d.get('source', 'SolarWinds') == source and not d.get('stale') and not d.get('carried'):
            reached_keys.add(d['key'])
            for it in d['hardware']:
                now['%s|%s' % (source, it['serial'])] = (it, d)
    prefix = source + '|'
    for key, rec in hist['parts'].items():
        if key.startswith(prefix) and key not in now and rec.get('host') in reached_keys:
            rec['checked'] = _checked(rec, hist) + 1         # looked for, not there
            rec['in_last'] = False
    for key, (it, d) in now.items():
        rec = hist['parts'].get(key)
        if rec is None:
            rec = hist['parts'][key] = {'first_seen': day, 'seen': 0, 'checked': 0, 'gaps': 0}
        else:
            rec['checked'] = _checked(rec, hist)
            if not rec.get('in_last', True):
                rec['gaps'] += 1                 # it was missing last time and is back
        rec.update(last_seen=day, in_last=True, pid=it['pid'], host=d['key'])
        rec['seen'] += 1
        rec['checked'] += 1
    annotate(devices, hist)
    return hist


def _checked(rec, hist):
    """Imports that looked for this part (older history stored 'first_import' instead)."""
    if 'checked' in rec:
        return rec['checked']
    return max(rec['seen'], hist['imports'].get('SolarWinds', 1) - rec.get('first_import', 1))


def annotate(devices, hist):
    """Copy first / last seen into each part (for the sheets and the app)."""
    for d in devices:
        src = d.get('source', 'SolarWinds')
        for it in d['hardware']:
            rec = (hist or {}).get('parts', {}).get('%s|%s' % (src, it['serial']))
            if not rec:
                continue
            it.update(first_seen=rec['first_seen'], last_seen=rec['last_seen'], seen=rec['seen'],
                      of=_checked(rec, hist), gaps=rec['gaps'])


def seen_text(it):
    """'5 of 6 imports, came back 1x' - for the Hardware sheet."""
    if not it.get('of'):
        return ''
    text = '%d of %d import%s' % (it['seen'], it['of'], '' if it['of'] == 1 else 's')
    return text + (', came back %dx' % it['gaps'] if it.get('gaps') else '')
