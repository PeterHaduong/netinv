"""NetInv desktop app (tkinter, standard library only).

Left: networks in the workspace - tick the ones to show and output, order them with the arrows -
and the imports: SolarWinds output, or the manual inventory sheet (isolated networks, bench
stock, devices SolarWinds never reaches). Right, tabs:
  Inventory    devices with their stack members / modules / FEX / PSUs underneath (expand,
               collapse, search by hostname, IP, serial or part number). Grey = SolarWinds did
               not reach it this round; its last known hardware is shown until it is forgotten.
               Exclusion rules hide another team's devices (e.g. on a shared SolarWinds)
  Part counts  every unique part number and how many there are, per network and in total
  Issues       everything the parser flagged, including what was left out for having no serial
  Changes      what changed since the previous inventory round (or the previous import)
  Outputs      Excel workbook, manual inventory sheet, sanitized summary report, settings
  SolarWinds commands   the command list to paste into an NCM "Execute Command Script" job
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

from . import VERSION
from .commands import CATALOG, script_text
from .history import DEFAULT_KEEP_DAYS
from .manual import template_sheets
from .model import SHARED_WARNING, display_name, excluded_by, in_inventory, part_counts
from .report import (workbook_for_networks, excluded_workbook, about_sheet, excluded_note,
                     device_kind, device_parts, feasibility_report)
from .kinds import BOX_KINDS, KIND_LABELS, RULE_KINDS, compile_rule, rule_hits, rule_list
from .roles import LABEL as ROLE_LABEL, compile_role_rule, role_from_hostname, role_label, role_name
from .workspace import Workspace, diff_snapshots, safe_name
from .xlsx import write_xlsx


def open_path(path):
    try:
        if hasattr(os, 'startfile'):
            os.startfile(path)
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


def _some(names, limit=3):
    """'A, B' or '11 devices (A, B, C, ...)' - for status messages that may name many devices."""
    names = list(names)
    if len(names) <= limit:
        return ', '.join(names)
    return '%d devices (%s, ...)' % (len(names), ', '.join(names[:limit]))


INPUT_DIR = None                    # the user's input/ folder: where the file pickers open (run())


def _start():
    """initialdir for file pickers: the input folder when there is one."""
    return {'initialdir': INPUT_DIR} if INPUT_DIR and os.path.isdir(INPUT_DIR) else {}


def _px(widget, n):
    """n pixels at 100 % Windows display scaling -> pixels at the current scaling."""
    return int(round(n * max(1.0, widget.winfo_fpixels('1i') / 96.0)))


def _wrap_to(container, label, less=0):
    """Wrap the label's text to the container's width (minus less px) as the window resizes."""
    container.bind('<Configure>', lambda e: label.config(wraplength=max(e.width - less, _px(label, 150))), add='+')


def _fit_screen(win, width=None, frac_w=0.95, reserve_h=110):
    """Size a window to what it asks for (or width px at 100 %, for one whose auto-sized tables
    would ask for very wide), never more than the screen has (small screens, high scaling);
    reserve_h: title bar + taskbar, in px at 100 %."""
    win.update_idletasks()
    w = min(_px(win, width) if width else win.winfo_reqwidth(), int(win.winfo_screenwidth() * frac_w))
    h = min(win.winfo_reqheight(), win.winfo_screenheight() - _px(win, reserve_h))
    win.geometry('%dx%d' % (w, h))


class ImportDialog(tk.Toplevel):
    """Network name + exports folder/file + optional SolarWinds node list."""

    def __init__(self, parent, networks):
        tk.Toplevel.__init__(self, parent)
        self.title('Import show-command exports')
        self.transient(parent)
        self.result = None
        self.v_name = tk.StringVar(value=networks[0] if networks else '')
        self.v_path = tk.StringVar()
        self.v_nodes = tk.StringVar()
        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Network name').grid(row=0, column=0, sticky='w', pady=4)
        ttk.Combobox(f, textvariable=self.v_name, values=networks, width=40).grid(row=0, column=1, sticky='we')
        ttk.Label(f, text='Exports (folder or file)').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Entry(f, textvariable=self.v_path, width=50).grid(row=1, column=1, sticky='we')
        b = ttk.Frame(f)
        b.grid(row=1, column=2, padx=4)
        ttk.Button(b, text='Folder...', command=lambda: self._pick(self.v_path, folder=True)).pack(side='left')
        ttk.Button(b, text='File...', command=lambda: self._pick(self.v_path)).pack(side='left')
        ttk.Label(f, text='Node list CSV (optional)').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Entry(f, textvariable=self.v_nodes, width=50).grid(row=2, column=1, sticky='we')
        ttk.Button(f, text='File...', command=lambda: self._pick(self.v_nodes, csv=True)).grid(row=2, column=2, sticky='w', padx=4)
        ttk.Label(f, foreground='#666', wraplength=_px(self, 520), text=(
            'Exports: SolarWinds NCM script results, terminal captures, or NCM config-archive '
            'folders - any mix. The node list is only needed if you want IP/location from a '
            'SolarWinds node export instead of the script results.')).grid(row=3, column=0, columnspan=3, sticky='w', pady=6)
        bb = ttk.Frame(f)
        bb.grid(row=4, column=0, columnspan=3, sticky='e')
        ttk.Button(bb, text='Import', command=self._ok).pack(side='left', padx=4)
        ttk.Button(bb, text='Cancel', command=self.destroy).pack(side='left')
        f.columnconfigure(1, weight=1)
        self.grab_set()

    def _pick(self, var, folder=False, csv=False):
        if folder:
            p = filedialog.askdirectory(parent=self, **_start())
        else:
            types = [('CSV', '*.csv'), ('All files', '*.*')] if csv else [('Text', '*.txt *.log'), ('All files', '*.*')]
            p = filedialog.askopenfilename(parent=self, filetypes=types, **_start())
        if p:
            var.set(p)

    def _ok(self):
        name, path = self.v_name.get().strip(), self.v_path.get().strip()
        if not name or not os.path.exists(path):
            messagebox.showwarning('Import', 'Enter a network name and choose the exports.', parent=self)
            return
        self.result = (name, path, self.v_nodes.get().strip() or None)
        self.destroy()


class ManualDialog(tk.Toplevel):
    """Manual inventory sheet + the network for rows whose Network column is blank."""

    def __init__(self, parent, networks):
        tk.Toplevel.__init__(self, parent)
        self.title('Import manual inventory sheet')
        self.transient(parent)
        self.result = None
        self.v_path = tk.StringVar()
        self.v_name = tk.StringVar(value=networks[0] if networks else '')
        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Manual sheet (.xlsx or .csv)').grid(row=0, column=0, sticky='w', pady=4)
        ttk.Entry(f, textvariable=self.v_path, width=50).grid(row=0, column=1, sticky='we')
        ttk.Button(f, text='File...', command=self._pick).grid(row=0, column=2, padx=4)
        ttk.Label(f, text='Network for rows with no Network').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Combobox(f, textvariable=self.v_name, values=networks, width=40).grid(row=1, column=1, sticky='we')
        ttk.Label(f, foreground='#666', wraplength=_px(self, 520), text=(
            'Each network on the sheet gets the sheet\'s rows as its manual devices (replacing the '
            'previous manual rows). Devices collected by SolarWinds are kept. Get a blank sheet '
            'from Outputs -> Blank manual sheet.')).grid(row=2, column=0, columnspan=3, sticky='w', pady=6)
        bb = ttk.Frame(f)
        bb.grid(row=3, column=0, columnspan=3, sticky='e')
        ttk.Button(bb, text='Import', command=self._ok).pack(side='left', padx=4)
        ttk.Button(bb, text='Cancel', command=self.destroy).pack(side='left')
        f.columnconfigure(1, weight=1)
        self.grab_set()

    def _pick(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[('Excel / CSV', '*.xlsx *.csv'), ('All files', '*.*')],
                                       **_start())
        if p:
            self.v_path.set(p)

    def _ok(self):
        path = self.v_path.get().strip()
        if not os.path.isfile(path):
            messagebox.showwarning('Import', 'Choose the manual inventory sheet.', parent=self)
            return
        self.result = (path, self.v_name.get().strip())
        self.destroy()


class ExclusionDialog(tk.Toplevel):
    """Exclusion rules for one network: hostname patterns, one per line, with a preview of the
    devices they match. Excluded devices are still imported - removing a rule brings them back."""

    def __init__(self, parent, app, network):
        tk.Toplevel.__init__(self, parent)
        self.app, self.network = app, network
        self.title('Exclusion rules - %s' % network)
        self.transient(parent)
        self.devices = app.ws.load(network, raw=True)['devices']
        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, wraplength=_px(self, 560), justify='left', text=(
            'Devices in %s whose name matches a rule are left out of the inventory, the counts and '
            'the workbook (the Excluded devices workbook lists them). One rule per line; * matches anything, '
            '? one character, any case. Examples:  PS-*   *-VG*   *PHONE*   campus-as1 (BLDG 2). '
            'A rule starting with ! keeps a device that another rule would exclude:  !PS-CORE1') % network
                  ).grid(row=0, column=0, columnspan=2, sticky='w')
        left, right = ttk.Frame(f), ttk.Frame(f)          # each with its own scrollbar (dozens of rules)
        left.grid(row=1, column=0, sticky='nsew', pady=8, padx=(0, 8))
        right.grid(row=1, column=1, sticky='nsew', pady=8)
        self.text = tk.Text(left, width=34, height=14, font=('Consolas', 10))
        ts = ttk.Scrollbar(left, orient='vertical', command=self.text.yview)
        self.text.configure(yscrollcommand=ts.set)
        self.text.pack(side='left', fill='both', expand=True)
        ts.pack(side='right', fill='y')
        self.text.insert('1.0', '\n'.join(app.ws.exclusions(network)))
        self.text.bind('<KeyRelease>', lambda e: self._preview())
        self.matches = ttk.Treeview(right, columns=('device', 'rule'), show='headings', height=14)
        for c, h, w in (('device', 'Matched device', 260), ('rule', 'Rule', 140)):
            self.matches.heading(c, text=h)
            self.matches.column(c, width=_px(self, w))
        ms = ttk.Scrollbar(right, orient='vertical', command=self.matches.yview)
        mx = ttk.Scrollbar(right, orient='horizontal', command=self.matches.xview)
        self.matches.configure(yscrollcommand=ms.set, xscrollcommand=mx.set)
        self.matches.grid(row=0, column=0, sticky='nsew')
        ms.grid(row=0, column=1, sticky='ns')
        mx.grid(row=1, column=0, sticky='ew')
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.count = ttk.Label(f, foreground='#555')
        self.count.grid(row=2, column=0, columnspan=2, sticky='w')
        bb = ttk.Frame(f)
        bb.grid(row=3, column=0, columnspan=2, sticky='e', pady=(8, 0))
        ttk.Button(bb, text='Save', command=self._save).pack(side='left', padx=4)
        ttk.Button(bb, text='Cancel', command=self.destroy).pack(side='left')
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)
        self._preview()
        _fit_screen(self, width=760)
        self.grab_set()

    def _rules(self):
        return [l.strip() for l in self.text.get('1.0', 'end').split('\n') if l.strip()]

    def _preview(self):
        rules = self._rules()
        self.matches.delete(*self.matches.get_children())
        hits = [(display_name(d), excluded_by(d, rules)) for d in self.devices]
        hits = sorted(h for h in hits if h[1])
        for name, rule in hits:
            self.matches.insert('', 'end', values=(name, rule))
        self.count.config(text='%d of %d devices excluded' % (len(hits), len(self.devices)))
        self.app._autosize(self.matches, cap=700)

    def _save(self):
        self.app.ws.set_exclusions(self.network, self._rules())
        self.destroy()
        self.app.refresh_networks()
        self.app.set_status('Exclusion rules for %s saved' % self.network)


class RulesDialog(tk.Toplevel):
    """Local rules (settings.json): what kind an unknown part is, and the role of a model.
    They are applied when the data is loaded - no re-import - and removing one undoes it.
    Each tab lists the rules and previews what the selected (or typed) rule matches."""

    FIELD_NAMES = (('pid', 'PID'), ('name', 'Inventory name'), ('descr', 'Description'),
                   ('any', 'Name or description'))

    def __init__(self, parent, app, pid=''):
        tk.Toplevel.__init__(self, parent)
        self.app = app
        self.title('Part and role rules')
        self.transient(parent)
        settings = app.ws.settings()
        # every entry is kept - one that cannot be used is listed as 'not valid' to fix or remove
        def entries(value):
            if value in (None, ''):
                return []
            return [list(r) if isinstance(r, tuple) else r for r in value] if isinstance(value, list) else [value]
        self.kind_rules = entries(settings.get('kind_rules'))
        self.role_rules = entries(settings.get('role_rules'))
        self.parts, self.devices = [], []       # as parsed, before any local rule
        for net in app.ws.networks():
            snap = app.ws.load(net, raw=True)
            for d in (snap or {}).get('devices', []):
                self.devices.append((net, d))
                self.parts += [(net, d, it) for it in d['hardware'] if it['kind'] not in BOX_KINDS]
        counts = {}                             # rule counts: each distinct part once, with its number
        for _, _, it in self.parts:
            k = (it.get('name', ''), it.get('descr', ''), it.get('pid', ''))
            counts[k] = counts.get(k, 0) + 1
        self.distinct = [({'name': k[0], 'descr': k[1], 'pid': k[2]}, n) for k, n in counts.items()]
        f = ttk.Frame(self, padding=10)
        f.pack(fill='both', expand=True)
        intro = ttk.Label(f, wraplength=_px(self, 700), justify='left', text=(
            'Rules for hardware NetInv does not know yet. They are kept in inventory_data\\settings.json, '
            'applied whenever data is loaded (no re-import) and listed on the workbook\'s About sheet. '
            'Patterns are regular expressions: ^ = starts with, $ = ends with, . = any character. '
            'The first matching rule wins.'))
        intro.pack(fill='x')
        _wrap_to(f, intro, _px(self, 24))
        bb = ttk.Frame(f)                      # packed before the tabs: never squeezed out
        bb.pack(side='bottom', fill='x')
        ttk.Button(bb, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(bb, text='Save', command=self._save).pack(side='right', padx=4)
        self.summary = ttk.Label(bb, foreground='#555')
        self.summary.pack(side='left', fill='x', expand=True)
        self.tabs = ttk.Notebook(f)
        self.tabs.pack(fill='both', expand=True, pady=8)
        self._build_kinds(pid)
        self._build_roles()
        self._fill_kinds()
        self._fill_roles()
        bb.lift()                                # Tab order: Save / Cancel last
        _fit_screen(self, width=980)
        self.minsize(min(self.k_form.winfo_reqwidth() + _px(self, 40), self.winfo_screenwidth() - _px(self, 20)),
                     min(_px(self, 420), self.winfo_screenheight() - _px(self, 110)))
        self.bind('<Escape>', lambda e: self.destroy())
        self.grab_set()
        if pid:
            self.k_pattern_entry.focus_set()

    # ------------------------------------------------------------- shared pieces
    def _table(self, parent, cols, heads, widths, height, stretch):
        """Treeview with both scrollbars."""
        box = ttk.Frame(parent)
        tree = ttk.Treeview(box, columns=cols, show='headings', height=height,
                            selectmode='browse' if height < 8 else 'none')
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=_px(self, w), minwidth=_px(self, 40), stretch=(c == stretch))
        ys = ttk.Scrollbar(box, orient='vertical', command=tree.yview)
        xs = ttk.Scrollbar(box, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        return box, tree

    def _form_buttons(self, row, add, update, remove, move):
        """Packed on the right before the pattern entry, so a narrow window shrinks the entry."""
        b = ttk.Frame(row)
        b.pack(side='right', padx=(8, 0))
        ttk.Button(b, text='Add', command=add).pack(side='left')
        ttk.Button(b, text='Update', command=update).pack(side='left', padx=2)
        ttk.Button(b, text='Remove', command=remove).pack(side='left')
        ttk.Button(b, text='▲', width=3, command=lambda: move(-1)).pack(side='left', padx=(6, 0))
        ttk.Button(b, text='▼', width=3, command=lambda: move(1)).pack(side='left')
        b.lift()                                 # Tab order: the pattern before these buttons

    PREVIEW_ROWS = 1000

    def _soon(self, which, fn):
        """Run a preview once typing pauses (200 ms): with many networks it scans every part."""
        jobs = self.__dict__.setdefault('_jobs', {})
        if jobs.get(which):
            self.after_cancel(jobs[which])
        jobs[which] = self.after(200, fn)

    @staticmethod
    def _picked(tree):
        sel = tree.selection()
        return tree.index(sel[0]) if sel else None

    def _move(self, rules, tree, fill, step):
        i = self._picked(tree)
        if i is None or not 0 <= i + step < len(rules):
            return
        rules[i], rules[i + step] = rules[i + step], rules[i]
        fill(select=i + step)

    # ------------------------------------------------------------- part kinds
    def _build_kinds(self, pid):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Part kinds')
        box, self.k_tree = self._table(tab, ('kind', 'field', 'pattern', 'n'),
                                       ('Kind', 'Matches on', 'Pattern', 'Parts'),
                                       (170, 140, 260, 60), 3, 'pattern')
        box.pack(fill='x')
        self.k_tree.bind('<<TreeviewSelect>>', lambda e: self._kind_selected())
        row = self.k_form = ttk.Frame(tab)
        row.pack(fill='x', pady=(8, 0))
        self.k_kind = tk.StringVar(value=KIND_LABELS['module'])
        self.k_field = tk.StringVar(value=self.FIELD_NAMES[0][1])
        self.k_pattern = tk.StringVar(value=('^%s$' % re.escape(pid).replace('\\-', '-')) if pid else '')
        ttk.Combobox(row, textvariable=self.k_kind, state='readonly', width=24,
                     values=[KIND_LABELS[k] for k in RULE_KINDS]).pack(side='left')
        ttk.Combobox(row, textvariable=self.k_field, state='readonly', width=18,
                     values=[n for _, n in self.FIELD_NAMES]).pack(side='left', padx=4)
        self._form_buttons(row, self._kind_add, self._kind_update, self._kind_remove,
                           lambda s: self._move(self.kind_rules, self.k_tree, self._fill_kinds, s))
        self.k_pattern_entry = ttk.Entry(row, textvariable=self.k_pattern, width=24)
        self.k_pattern_entry.pack(side='left', fill='x', expand=True)
        self.k_pattern_entry.bind('<Return>', lambda e: self._kind_add())
        for v in (self.k_kind, self.k_field, self.k_pattern):
            v.trace_add('write', lambda *a: self._soon('kind', self._kind_preview))
        self.k_error = ttk.Label(tab, foreground='#C00000')
        self.k_error.pack(fill='x', pady=(2, 0))
        ttk.Label(tab, text='Parts the pattern above matches (as NetInv classifies them):').pack(anchor='w', pady=(6, 2))
        box, self.k_hits = self._table(tab, ('net', 'device', 'part', 'pid', 'descr', 'now'),
                                       ('Network', 'Device', 'Inventory name', 'PID', 'Description', 'NetInv kind'),
                                       (90, 150, 200, 150, 220, 130), 8, 'descr')
        box.pack(fill='both', expand=True)

    def _kind_form(self):
        """The rule being typed, compiled, or None with the problem shown."""
        kind = dict((v, k) for k, v in KIND_LABELS.items()).get(self.k_kind.get(), '')
        field = dict((n, k) for k, n in self.FIELD_NAMES).get(self.k_field.get(), '')
        rule = [kind, field, self.k_pattern.get().strip()]
        try:
            compile_rule(rule)
        except (ValueError, re.error) as exc:
            self.k_error.config(text=str(exc) if rule[2] else '')
            return None
        self.k_error.config(text='')
        return rule

    def _kind_preview(self):
        self.k_hits.delete(*self.k_hits.get_children())
        rule = self._kind_form()
        if not rule:
            return
        r = compile_rule(rule)
        hits = [(net, d, it) for net, d, it in self.parts if rule_hits(r, it)]
        for net, d, it in hits[:self.PREVIEW_ROWS]:
            self.k_hits.insert('', 'end', values=(net, display_name(d), it['name'], it['pid'], it['descr'],
                                                  KIND_LABELS.get(it['kind'], it['kind'])))
        if len(hits) > self.PREVIEW_ROWS:
            self.k_hits.insert('', 'end', values=('', '... and %d more parts' % (len(hits) - self.PREVIEW_ROWS),
                                                  '', '', '', ''))
        self.app._autosize(self.k_hits, stretch='descr', max_rows=500)

    def _fill_kinds(self, select=None):
        self.k_tree.delete(*self.k_tree.get_children())
        firsts = [0] * len(self.kind_rules)
        compiled = []
        for r in self.kind_rules:
            try:
                compiled.append(compile_rule(r))
            except (ValueError, re.error, TypeError):
                compiled.append(None)
        for it, n in self.distinct:                 # the first matching rule names the part
            for i, c in enumerate(compiled):
                if c and rule_hits(c, it):
                    firsts[i] += n
                    break
        for r, c, n in zip(self.kind_rules, compiled, firsts):
            if c:
                values = (KIND_LABELS[c[0]], dict(self.FIELD_NAMES)[c[1]], c[2].pattern, n)
            else:
                values = ('(not valid)', '', json.dumps(r), '-')
            self.k_tree.insert('', 'end', values=values)
        self.app._autosize(self.k_tree, cap=600, stretch='pattern', max_rows=500)
        kids = self.k_tree.get_children()
        if select is not None and kids:
            self.k_tree.selection_set(kids[min(select, len(kids) - 1)])
            self.k_tree.see(kids[min(select, len(kids) - 1)])
        self._kind_preview()
        self._summary()

    def _kind_selected(self):
        i = self._picked(self.k_tree)
        if i is None:
            return
        r = self.kind_rules[i]
        try:
            compile_rule(r)
        except (ValueError, re.error, TypeError):
            return
        self.k_kind.set(KIND_LABELS.get(r[0], r[0]))
        self.k_field.set(dict(self.FIELD_NAMES).get(r[1], r[1]))
        self.k_pattern.set(r[2])

    def _kind_add(self):
        rule = self._kind_form()
        if rule and rule not in self.kind_rules:
            self.kind_rules.append(rule)
            self._fill_kinds(select=len(self.kind_rules) - 1)

    def _kind_update(self):
        i, rule = self._picked(self.k_tree), self._kind_form()
        if i is not None and rule and rule not in self.kind_rules[:i] + self.kind_rules[i + 1:]:
            self.kind_rules[i] = rule
            self._fill_kinds(select=i)

    def _kind_remove(self):
        i = self._picked(self.k_tree)
        if i is not None:
            del self.kind_rules[i]
            self._fill_kinds(select=i)

    # ------------------------------------------------------------- roles
    def _build_roles(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Roles')
        note = ttk.Label(tab, wraplength=_px(self, 680), justify='left', foreground='#555', text=(
            'The Role column of the workbook\'s Devices sheet comes from the hostname first (AS = access, '
            'LF = leaf ...). These rules give a role from the model when the hostname does not. '
            'Pick a role or type your own.'))
        note.pack(fill='x', pady=(0, 6))
        _wrap_to(tab, note, _px(self, 20))
        box, self.r_tree = self._table(tab, ('role', 'pattern', 'n'), ('Role', 'Model pattern', 'Devices'),
                                       (200, 300, 70), 3, 'pattern')
        box.pack(fill='x')
        self.r_tree.bind('<<TreeviewSelect>>', lambda e: self._role_selected())
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=(8, 0))
        self.r_role = tk.StringVar()
        self.r_pattern = tk.StringVar()
        ttk.Combobox(row, textvariable=self.r_role, width=26,
                     values=sorted(set(ROLE_LABEL.values()))).pack(side='left')
        self._form_buttons(row, self._role_add, self._role_update, self._role_remove,
                           lambda s: self._move(self.role_rules, self.r_tree, self._fill_roles, s))
        r_entry = ttk.Entry(row, textvariable=self.r_pattern, width=24)
        r_entry.pack(side='left', padx=4, fill='x', expand=True)
        r_entry.bind('<Return>', lambda e: self._role_add())
        for v in (self.r_role, self.r_pattern):
            v.trace_add('write', lambda *a: self._soon('role', self._role_preview))
        self.r_error = ttk.Label(tab, foreground='#C00000')
        self.r_error.pack(fill='x', pady=(2, 0))
        ttk.Label(tab, text='Devices whose model the pattern above matches:').pack(anchor='w', pady=(6, 2))
        box, self.r_hits = self._table(tab, ('net', 'device', 'model', 'now', 'result'),
                                       ('Network', 'Device', 'Model', 'Role now', 'With this rule'),
                                       (90, 170, 200, 160, 220), 8, 'result')
        box.pack(fill='both', expand=True)

    def _role_form(self):
        role = self.r_role.get().strip()
        role = dict((v, k) for k, v in ROLE_LABEL.items()).get(role, role)
        rule = [role, self.r_pattern.get().strip()]
        try:
            if not rule[0] or not rule[1]:
                raise ValueError('enter a role and a model pattern' if rule[0] or rule[1] else '')
            re.compile(rule[1])
        except (ValueError, re.error) as exc:
            self.r_error.config(text=str(exc))
            return None
        self.r_error.config(text='')
        return rule

    def _role_preview(self):
        self.r_hits.delete(*self.r_hits.get_children())
        rule = self._role_form()
        if not rule:
            return
        rx = re.compile(rule[1])
        hits = [(net, d) for net, d in self.devices if in_inventory(d) and rx.search(d['model'] or '')]
        if len(hits) > self.PREVIEW_ROWS:
            self.r_hits.insert('', 'end', values=('', '%d devices - the first %d:' % (len(hits), self.PREVIEW_ROWS),
                                                  '', '', ''))
        for net, d in hits[:self.PREVIEW_ROWS]:
            by_host = role_from_hostname(d['hostname'])
            self.r_hits.insert('', 'end', values=(
                net, display_name(d), d['model'], role_label(d['hostname'], d['model']) or '-',
                ('%s (hostname wins)' % role_name(by_host)) if by_host else role_name(rule[0])))
        self.app._autosize(self.r_hits, stretch='result', max_rows=500)

    def _fill_roles(self, select=None):
        self.r_tree.delete(*self.r_tree.get_children())
        for r in self.role_rules:
            try:
                role, rx = compile_role_rule(r)
                n = sum(1 for _, d in self.devices if in_inventory(d) and rx.search(d['model'] or ''))
                values = (role_name(role), rx.pattern, n)
            except (ValueError, re.error, TypeError):
                values = ('(not valid)', json.dumps(r), '-')
            self.r_tree.insert('', 'end', values=values)
        self.app._autosize(self.r_tree, cap=600, stretch='pattern', max_rows=500)
        kids = self.r_tree.get_children()
        if select is not None and kids:
            self.r_tree.selection_set(kids[min(select, len(kids) - 1)])
            self.r_tree.see(kids[min(select, len(kids) - 1)])
        self._role_preview()
        self._summary()

    def _role_selected(self):
        i = self._picked(self.r_tree)
        try:
            role, rx = compile_role_rule(self.role_rules[i]) if i is not None else (None, None)
        except (ValueError, re.error, TypeError):
            return
        if role:
            self.r_role.set(role_name(role))
            self.r_pattern.set(rx.pattern)

    def _role_add(self):
        rule = self._role_form()
        if rule and rule not in self.role_rules:
            self.role_rules.append(rule)
            self._fill_roles(select=len(self.role_rules) - 1)

    def _role_update(self):
        i, rule = self._picked(self.r_tree), self._role_form()
        if i is not None and rule and rule not in self.role_rules[:i] + self.role_rules[i + 1:]:
            self.role_rules[i] = rule
            self._fill_roles(select=i)

    def _role_remove(self):
        i = self._picked(self.r_tree)
        if i is not None:
            del self.role_rules[i]
            self._fill_roles(select=i)

    # ------------------------------------------------------------- save
    def _summary(self):
        if hasattr(self, 'summary'):
            self.summary.config(text='%d part rule%s, %d role rule%s - not saved until you click Save'
                                % (len(self.kind_rules), '' if len(self.kind_rules) == 1 else 's',
                                   len(self.role_rules), '' if len(self.role_rules) == 1 else 's'))

    def _save(self):
        pending = []
        k, r = self._kind_form(), self._role_form()
        if k and k not in self.kind_rules:
            pending.append(('part', k))
        if r and r not in self.role_rules:
            pending.append(('role', r))
        if pending:
            ans = messagebox.askyesnocancel('Part and role rules', 'Add the rule you typed before saving?\n\n'
                                            + '\n'.join('%s rule: %s' % (w, ' / '.join(x)) for w, x in pending),
                                            parent=self)
            if ans is None:
                return
            if ans:
                self.kind_rules += [x for w, x in pending if w == 'part']
                self.role_rules += [x for w, x in pending if w == 'role']
        self.app.ws.set_rules(self.kind_rules, self.role_rules)
        self.destroy()
        self.app.refresh_networks()
        problems = getattr(self.app.ws, 'rule_problems', None)
        self.app.set_status('settings.json: ' + '; '.join(problems) if problems else
                            'Rules saved: %d part, %d role' % (len(self.kind_rules), len(self.role_rules)))


class App(object):
    def __init__(self, root, workspace_dir):
        self.root = root
        self.ws = Workspace(workspace_dir)
        self.out_dir = tk.StringVar(value=os.path.join(workspace_dir, 'outputs'))
        self.snaps = {}                 # network -> latest snapshot
        self.msgs = queue.Queue()
        root.title('NetInv %s - network inventory' % VERSION)
        try:
            ttk.Style().theme_use('vista')
        except tk.TclError:
            pass
        self._setup_scaling()
        self._build()
        self.refresh_networks()
        self._pump()

    # ------------------------------------------------------------------ layout
    def _setup_scaling(self):
        """Windows display scaling (125 %, 150 % ...): fonts grow with it, so the table row height
        comes from the font metrics and column widths are measured (_autosize), never fixed pixels."""
        self.scale = max(1.0, self.root.winfo_fpixels('1i') / 96.0)
        self.font_body = tkfont.nametofont('TkDefaultFont')
        self.font_head = tkfont.nametofont('TkHeadingFont')
        self.font_device = tkfont.Font(family='Segoe UI', size=9, weight='bold')
        self.font_network = tkfont.Font(family='Segoe UI', size=10, weight='bold')
        self.font_group = tkfont.Font(family='Segoe UI', size=9, weight='bold', slant='italic')
        line = max(f.metrics('linespace') for f in (self.font_body, self.font_device, self.font_network))
        style = ttk.Style()
        style.configure('Treeview', rowheight=line + self.px(3))
        self.indent = self.px(20)
        style.configure('Treeview', indent=self.indent)
        w, h = self.px(1280), self.px(760)
        self.root.geometry('%dx%d' % (min(w, int(self.root.winfo_screenwidth() * 0.95)),
                                      min(h, self.root.winfo_screenheight() - self.px(110))))

    def px(self, n):
        """n pixels at 100 % scaling -> pixels at the current scaling."""
        return int(round(n * self.scale))

    def _autosize(self, tree, cap=380, wide_cap=None, max_rows=4000, stretch=None, names=(), name_cap=700):
        """Fit each column to its heading and contents (measured with the fonts in use).
        The stretch column (default: the last - notes / details) is as wide as its longest text
        (wide_cap: limit it), so the horizontal scrollbar reaches the end; it also takes up the
        rest of the window. names: columns holding device names, allowed up to name_cap."""
        cols = list(tree['columns'])
        has_tree = 'tree' in str(tree.cget('show'))
        pad = self.px(18)
        widths = {}
        for c in (['#0'] if has_tree else []) + cols:
            widths[c] = self.font_head.measure(tree.heading(c, 'text')) + pad
        todo, seen = [(iid, 0) for iid in tree.get_children('')], 0
        while todo and seen < max_rows:
            iid, depth = todo.pop()
            seen += 1
            tags = tree.item(iid, 'tags') or ()
            font = (self.font_network if 'network' in tags else self.font_group if 'group' in tags else
                    self.font_device if 'device' in tags else self.font_body)
            if 'group' in tags:                 # explanatory row: does not size the columns
                todo.extend((child, depth + 1) for child in tree.get_children(iid))
                continue
            if has_tree:
                text = tree.item(iid, 'text')
                widths['#0'] = max(widths['#0'], (depth + 1) * self.indent + font.measure(text) + pad)
            for c, v in zip(cols, tree.item(iid, 'values') or ()):
                widths[c] = max(widths[c], font.measure(str(v)) + pad)
            todo.extend((child, depth + 1) for child in tree.get_children(iid))
        last = stretch or (cols[-1] if cols else None)
        for c, w in widths.items():
            limit = wide_cap if c == last else (name_cap if c in names else cap)
            width = w if limit is None else min(w, self.px(limit))
            # the stretch column's minwidth = its width: ttk would otherwise shrink it to fit the
            # widget whenever the table is refilled on screen (notes cut to one letter)
            tree.column(c, width=width, minwidth=width if c == last else self.px(20), stretch=(c == last))

    def _build(self):
        # Left panel packed first so it always keeps its width; the tabs take the rest (the
        # auto-sized tables can ask for more than the window has and scroll instead).
        self.status = ttk.Label(self.root, text='Ready', anchor='w', padding=(8, 2))
        self.status.pack(fill='x', side='bottom')        # before the body: never squeezed out
        body = ttk.Frame(self.root)
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body, padding=8)
        left.pack(side='left', fill='y')
        head = ttk.Frame(left)
        head.pack(fill='x')
        ttk.Label(head, text='Networks', font=('Segoe UI', 11, 'bold')).pack(side='left')
        ttk.Button(head, text='None', width=5, command=lambda: self.set_checked([])).pack(side='right')
        ttk.Button(head, text='All', width=4, command=lambda: self.set_checked(self.order)).pack(side='right', padx=2)
        # One row per network: the box shows / hides it (everywhere, and in the outputs); clicking
        # the name picks it for Move / Export / Remove / Exclusion rules; double-click = only this.
        self.netlist = ttk.Treeview(left, show='tree', selectmode='browse', height=4)    # grows with the window
        self.netlist.column('#0', width=self.px(240))
        self.netlist.pack(fill='both', expand=True, pady=4)
        self.netlist.bind('<Button-1>', self._net_click)
        self.netlist.bind('<Double-1>', self._net_only)
        self.netlist.bind('<<TreeviewSelect>>', lambda e: self._show_netinfo())
        self.netlist.bind('<space>', lambda e: self.toggle_network(self._current()))
        move = ttk.Frame(left)
        move.pack(fill='x')
        ttk.Button(move, text='▲ Move up', command=lambda: self.move_network(-1)).pack(side='left', expand=True, fill='x')
        ttk.Button(move, text='▼ Move down', command=lambda: self.move_network(1)).pack(side='left', expand=True, fill='x', padx=(2, 0))
        ttk.Label(left, foreground='#666', wraplength=self.px(240), justify='left', text=(
            'Tick a box to show that network. Click a name to pick it for the buttons; '
            'double-click to show only that one.')).pack(fill='x', pady=(4, 8))
        for text, cmd in (('Import SolarWinds output...', self.do_import),
                          ('Import manual sheet...', self.do_import_manual),
                          ('Import snapshot file...', self.do_import_snapshot),
                          ('Export snapshot file...', self.do_export_snapshot),
                          ('Remove network', self.do_remove)):
            ttk.Button(left, text=text, command=cmd).pack(fill='x', pady=2)
        self.netinfo = ttk.Label(left, text='', foreground='#555', wraplength=self.px(230), justify='left')
        self.netinfo.pack(fill='x', pady=8)

        self.tabs = ttk.Notebook(body, width=self.px(600))
        self.tabs.pack(side='left', fill='both', expand=True)
        self._build_inventory()
        self.counts = self._simple_tab('Part counts', ('Kind', 'PID', 'Description', 'Total'),
                                       (150, 200, 380, 70))
        self.issues = self._simple_tab('Issues', ('Network', 'Device', 'Issue'), (120, 200, 700))
        self._build_changes()
        self._build_outputs()
        self._build_commands()

    def _build_inventory(self):
        tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(tab, text='Inventory')
        bar = ttk.Frame(tab)
        bar.pack(fill='x')
        self.inv_count = ttk.Label(bar, text='')
        self.inv_count.pack(side='right')
        ttk.Label(bar, text='Search').pack(side='left')
        self.search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.search, width=30)
        e.pack(side='left', padx=6)
        e.bind('<KeyRelease>', lambda ev: self._search_soon())
        ttk.Button(bar, text='Expand all', command=lambda: self._expand(True)).pack(side='left', padx=2)
        ttk.Button(bar, text='Collapse all', command=lambda: self._expand(False)).pack(side='left', padx=2)
        ttk.Button(bar, text='Exclusion rules...', command=self.do_exclusion_rules).pack(side='left', padx=(12, 2))
        acts = ttk.Frame(tab)                    # second row: what to do with the selected device(s)
        acts.pack(fill='x', pady=(4, 0))
        ttk.Label(acts, text='Selected device:').pack(side='left')
        self.excl_button = ttk.Button(acts, text='Exclude selected device', command=self.do_exclude_selected)
        self.excl_button.pack(side='left', padx=(6, 2))
        ttk.Button(acts, text='Forget (gone for good)...', command=self.do_forget).pack(side='left', padx=2)
        self.shared_button = ttk.Button(acts, text='Mark serial as shared/default', command=self.do_shared_serial)
        self.shared_button.pack(side='left', padx=(12, 2))
        cols = ('kind', 'model', 'serial', 'uplink', 'software', 'status', 'ip', 'location', 'notes')
        heads = ('Kind', 'Model / PID', 'Serial', 'Uplink module', 'Software', 'Status / role',
                 'Mgmt IP', 'SNMP location', 'Notes')
        widths = (150, 210, 110, 110, 120, 110, 110, 170, 300)
        frame = ttk.Frame(tab)
        frame.pack(fill='both', expand=True, pady=4)
        self.tree = ttk.Treeview(frame, columns=cols)
        self.tree.heading('#0', text='Device / unit')
        self.tree.column('#0', width=self.px(230), stretch=False)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=self.px(w), stretch=(c == 'notes'))
        self.tree.tag_configure('network', font=self.font_network, background='#DDEBF7')
        self.tree.tag_configure('device', font=self.font_device)
        self.tree.tag_configure('warn', background='#FFF2CC')
        self.tree.tag_configure('stale', foreground='#808080')
        self.tree.tag_configure('excluded', foreground='#808080')
        self.tree.tag_configure('group', foreground='#808080', font=self.font_group)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._update_exclude_button())
        self.tree.bind('<Double-1>', self._tree_double)
        ys = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _build_changes(self):
        tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(tab, text='Changes')
        bar = ttk.Frame(tab)
        bar.pack(fill='x', pady=(0, 4))
        ttk.Label(bar, text='Compare with').pack(side='left')
        self.compare = tk.StringVar(value=self.COMPARE[0])
        box = ttk.Combobox(bar, textvariable=self.compare, values=self.COMPARE, state='readonly', width=48)
        box.pack(side='left', padx=6)
        box.bind('<<ComboboxSelected>>', lambda e: self.fill_changes())
        heads, widths = ('Network', 'Compared with', 'Change', 'Device', 'Detail'), (110, 130, 140, 190, 480)
        frame = ttk.Frame(tab)
        frame.pack(fill='both', expand=True)
        self.changes = ttk.Treeview(frame, columns=heads, show='headings')
        for h, w in zip(heads, widths):
            self.changes.heading(h, text=h)
            self.changes.column(h, width=self.px(w), stretch=(h == 'Detail'))
        ys = ttk.Scrollbar(frame, orient='vertical', command=self.changes.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=self.changes.xview)
        self.changes.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.changes.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    COMPARE = ('The previous inventory round (30+ days earlier)', 'The previous import (e.g. before a retry)')

    def _simple_tab(self, title, heads, widths):
        tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(tab, text=title)
        tree = ttk.Treeview(tab, columns=heads, show='headings')
        # A note above the table (e.g. Part counts: what is not counted), shown only when set;
        # it wraps to the tab's width.
        tree.note = ttk.Label(tab, foreground='#7F6000', justify='left')
        tree.note.grid(row=0, column=0, columnspan=2, sticky='we', pady=(0, 6))
        tree.note.grid_remove()
        tab.bind('<Configure>', lambda e: tree.note.config(wraplength=max(e.width - self.px(20), 200)))
        for h, w in zip(heads, widths):
            tree.heading(h, text=h)
            tree.column(h, width=self.px(w), stretch=(h == heads[-1]))
        ys = ttk.Scrollbar(tab, orient='vertical', command=tree.yview)
        xs = ttk.Scrollbar(tab, orient='horizontal', command=tree.xview)   # many network columns
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=1, column=0, sticky='nsew')
        ys.grid(row=1, column=1, sticky='ns')
        xs.grid(row=2, column=0, sticky='ew')
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)
        return tree

    def _build_outputs(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text='Outputs')
        ttk.Label(tab, text='Uses the networks selected on the left (or all, if none are selected).',
                  foreground='#555').grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(tab, text='Output folder').grid(row=1, column=0, sticky='w')
        ttk.Entry(tab, textvariable=self.out_dir, width=70).grid(row=1, column=1, sticky='we', padx=6)
        ttk.Button(tab, text='Browse...', command=lambda: self.out_dir.set(
            filedialog.askdirectory() or self.out_dir.get())).grid(row=1, column=2)
        actions = [
            ('Excel workbook', 'One grouped sheet per network + sortable Devices / Hardware / Part counts tables', self.do_workbook),
            ('Excluded devices workbook', 'Separate file: only the devices left out by exclusion rules (not in any inventory count)', self.do_excluded_workbook),
            ('Blank manual sheet', 'Excel sheet to type in hardware by hand (isolated networks, bench stock)', lambda: self.do_manual_sheet(False)),
            ('Manual sheet to edit', 'The selected networks\' manual rows, to change and import again', lambda: self.do_manual_sheet(True)),
            ('Sanitized summary', 'Counts only - no hostnames, IPs, serials (for parser troubleshooting)', self.do_summary),
            ('Open output folder', '', lambda: self._open_out()),
        ]
        hints = []
        for i, (label, hint, cmd) in enumerate(actions):
            b = ttk.Button(tab, text=label, command=cmd)
            b.grid(row=2 + i, column=0, sticky='we', pady=2)
            hints.append(ttk.Label(tab, text=hint, foreground='#555', justify='left'))
            hints[-1].grid(row=2 + i, column=1, columnspan=2, sticky='w')
        tab.bind('<Configure>', lambda e: [h.config(wraplength=max(e.width - b.winfo_reqwidth() - self.px(40),
                                                                   self.px(150))) for h in hints], add='+')
        opts = ttk.LabelFrame(tab, text='Settings', padding=8)
        opts.grid(row=2 + len(actions), column=0, columnspan=3, sticky='we', pady=(8, 0))
        self.optics = tk.BooleanVar(value=self.ws.include_optics())
        ttk.Checkbutton(opts, text='Include optics (SFPs) - from devices and spares on the manual sheet',
                        variable=self.optics, command=self.do_optics).grid(row=0, column=0, columnspan=4, sticky='w')
        days = self.ws.settings().get('keep_unreached_days', DEFAULT_KEEP_DAYS)
        self.keep_days = tk.StringVar(value='' if days is None else str(days))
        ttk.Label(opts, text='Devices SolarWinds cannot reach keep their last known hardware for:').grid(
            row=1, column=0, sticky='w', pady=(6, 0))
        keep = ttk.Frame(opts)
        keep.grid(row=2, column=0, sticky='w', pady=(2, 0))
        ttk.Entry(keep, textvariable=self.keep_days, width=6).pack(side='left')
        ttk.Label(keep, text='days (blank = until you forget them)').pack(side='left', padx=6)
        ttk.Button(keep, text='Save', command=self.do_save_keep).pack(side='left', padx=4)
        rules = ttk.Frame(opts)
        rules.grid(row=3, column=0, sticky='w', pady=(10, 0))
        ttk.Button(rules, text='Part / role rules...', command=lambda: self.do_rules()).pack(side='left')
        ttk.Label(rules, foreground='#555', text='name parts and models NetInv does not know yet').pack(
            side='left', padx=6)
        tab.columnconfigure(1, weight=1)

    def _build_commands(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text='SolarWinds commands')
        intro = ttk.Label(tab, wraplength=self.px(760), justify='left', text=(
            'Paste this list into a SolarWinds NCM "Execute Command Script" job (or a scheduled '
            'command-script job) and run it against every device. It is one list for all '
            'platforms: a device that does not support a command (e.g. "show module" on a C8300, '
            '"show fex" on IOS) answers with an error, and NetInv skips that section. It starts '
            'with "terminal length 0" and has a "!" line between commands, so it can also be pasted '
            'into an SSH session by hand without a command being lost.'))
        intro.pack(anchor='w', fill='x')
        _wrap_to(tab, intro, self.px(30))
        body = ttk.Frame(tab)
        body.pack(fill='both', expand=True, pady=8)
        left = ttk.Frame(body)
        left.pack(side='top', fill='x')
        self.cmd_vars = []
        descs, boxes = [], []
        for i, (cmd, why, where, on) in enumerate(CATALOG):
            v = tk.BooleanVar(value=on)
            self.cmd_vars.append((cmd, v))
            boxes.append(ttk.Checkbutton(left, text=cmd, variable=v, command=self._refresh_script))
            boxes[-1].grid(row=i, column=0, sticky='nw', pady=1)
            descs.append(ttk.Label(left, text='%s  [%s]' % (why, where), foreground='#555', justify='left'))
            descs[-1].grid(row=i, column=1, sticky='w', padx=(8, 0), pady=1)
        left.columnconfigure(1, weight=1)
        left.bind('<Configure>', lambda e: [d.config(wraplength=max(
            e.width - max(b.winfo_reqwidth() for b in boxes) - self.px(20), self.px(150))) for d in descs])
        right = ttk.Frame(body)
        right.pack(side='top', fill='both', expand=True, pady=(10, 0))
        ttk.Label(right, text='Script to paste:').pack(anchor='w')
        bb = ttk.Frame(right)                    # packed before the text: never squeezed out
        bb.pack(side='bottom', fill='x', pady=4)
        self.script = tk.Text(right, height=4, width=60, font=('Consolas', 10))
        self.script.pack(fill='both', expand=True)
        ttk.Button(bb, text='Copy to clipboard', command=self._copy_script).pack(side='left')
        ttk.Button(bb, text='Save as .txt...', command=self._save_script).pack(side='left', padx=6)
        self._refresh_script()

    # ------------------------------------------------------------------ data
    def selected_networks(self):
        """The ticked networks, in the user's order."""
        return [n for n in self.order if n in self.checked and n in self.snaps]

    def _load(self, net, index=-1):
        """ws.load, remembered while the snapshot file and the settings that shape the view
        (rules, exclusions, optics, shared serials) are unchanged. The views are only read here."""
        files = self.ws.snapshots(net)
        if not files or abs(index) > len(files):
            return None
        path = files[index]
        settings = dict(self.ws.settings())
        for k in ('checked_networks', 'known_networks', 'network_order'):     # not part of the view
            settings.pop(k, None)
        exclude = settings.pop('exclude', None)
        settings['exclude'] = exclude.get(net) if isinstance(exclude, dict) else exclude   # this network's only
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = None
        key = (net, path, stamp, json.dumps(settings, sort_keys=True, default=str))
        cache = self.__dict__.setdefault('_snap_cache', {})
        if key not in cache:
            cache[key] = self.ws.load(net, index)
        self._cache_used.add(key)
        return cache[key]

    def refresh_networks(self, select=None):
        """Reload every network (after an import, a rule change ...) and redraw. Snapshots whose
        file and view settings did not change come from memory (_load)."""
        self.snaps = {}
        self._cache_used = set()
        for n in self.ws.networks():
            try:
                self.snaps[n] = self._load(n)
            except Exception as exc:
                self.set_status('Could not read %s: %s' % (n, exc))
        for n in self.snaps:                      # the baselines Changes compares with
            for mode in ('round', 'previous'):
                i = self.ws.baseline_index(n, mode)
                if i is not None:
                    try:
                        self._load(n, i)
                    except Exception:
                        pass
        cache = self.__dict__.get('_snap_cache', {})
        for k in [k for k in cache if k not in self._cache_used]:     # forget what is gone
            del cache[k]
        self.order = [n for n in self.ws.networks() if n in self.snaps]
        if getattr(self.ws, 'rule_problems', None):
            self.set_status('settings.json: ' + '; '.join(self.ws.rule_problems))
        self.checked = set(self.ws.checked_networks())
        if select:
            self.checked.add(select)
        self._draw_networks(select or self._current())
        self.show_selected()

    def _draw_networks(self, pick=None):
        self.netlist.delete(*self.netlist.get_children())
        for n in self.order:
            self.netlist.insert('', 'end', iid=n, text='%s  %s' % ('☑' if n in self.checked else '☐', n))
        if pick in self.order:
            self.netlist.selection_set(pick)
            self.netlist.see(pick)

    def _current(self):
        sel = self.netlist.selection()
        return sel[0] if sel else None

    def _net_click(self, event):
        n = self.netlist.identify_row(event.y)
        if not n:
            return
        box = self.netlist.bbox(n, '#0')
        if box and event.x < box[0] + self.indent + self.font_body.measure('☑  ') + self.px(4):
            self.toggle_network(n)
            return 'break'

    def _net_only(self, event):
        n = self.netlist.identify_row(event.y)
        if n:
            self.set_checked([n])
            self.netlist.selection_set(n)
        return 'break'

    def toggle_network(self, n):
        if n:
            self.set_checked([x for x in self.order if (x in self.checked) != (x == n)], pick=n)

    def set_checked(self, names, pick=None):
        self.checked = set(names)
        self.ws.set_checked_networks([n for n in self.order if n in self.checked])
        self._draw_networks(pick or self._current())
        self.show_selected()

    def move_network(self, step):
        n = self._current()
        if not n:
            messagebox.showinfo('Move', 'Click a network name first.')
            return
        i = self.order.index(n)
        j = min(max(i + step, 0), len(self.order) - 1)
        self.order.insert(j, self.order.pop(i))
        self.ws.set_network_order(self.order)
        self._draw_networks(n)
        self.show_selected()

    def show_selected(self):
        self._show_netinfo()
        self.fill_inventory()
        self.fill_counts()
        self.fill_issues()
        self.fill_changes()

    def _show_netinfo(self):
        """The ticked networks under the list; with more than 3, one summary line plus the
        picked network, so the panel keeps its buttons."""
        lines = {}
        for n in self.selected_networks():
            s = self.snaps[n]
            excl, count = len(s.get('excluded', [])), len(self.ws.snapshots(n))
            devs = sum(1 for d in s['devices'] if in_inventory(d))
            lines[n] = ('%s: %d device%s%s, imported %s (%d snapshot%s)'
                        % (n, devs, '' if devs == 1 else 's',
                           (' (%d excluded)' % excl) if excl else '', s['created'][:16],
                           count, '' if count == 1 else 's'))
        info = [lines[n] for n in self.selected_networks()]
        if len(info) > 3:
            total = sum(sum(1 for d in self.snaps[n]['devices'] if in_inventory(d)) for n in lines)
            info = ['%d networks ticked, %d devices in all.' % (len(lines), total)]
            if self._current() in lines:
                info.append(lines[self._current()])
        self.netinfo.config(text='\n'.join(info) or (
            'No networks ticked.' if self.order else 'No networks yet - click "Import SolarWinds output...".'))

    def _search_soon(self):
        """Redraw once typing pauses (250 ms), not on every key: a broad search opens every
        matching device, which is slow with thousands of devices."""
        if getattr(self, '_search_job', None):
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(250, self._search_now)

    def _search_now(self):
        self._search_job = None
        self.fill_inventory()

    def _tree_state(self):
        """What the user has open / selected / scrolled to, by (network, device key), so a
        refresh (forget, exclude, ticking a network ...) does not throw it away."""
        nodes = getattr(self, 'inv_nodes', {})
        groups, devs = getattr(self, 'group_rows', {}), getattr(self, 'dev_rows', set())
        rows = self._visible_rows()
        top = next((i for i, iid in enumerate(rows) if self.tree.bbox(iid)), 0)
        state = {'nets': {}, 'devices': set(), 'selected': set(), 'gone_at': {}, 'top_index': top,
                 'top': self._row_id(rows[top]) if rows else None, 'q': getattr(self, '_last_q', None)}
        selected = set(self.tree.selection())      # one call: it walks the whole tree
        for nid in self.tree.get_children():
            net = nodes.get(nid, (None, None))[0]
            state['nets'][net] = bool(self.tree.item(nid, 'open'))
            lists = [(net, self.tree.get_children(nid))]
            for gid in self.tree.get_children(nid):
                if gid in groups:
                    state['nets'][(net, 'excluded')] = bool(self.tree.item(gid, 'open'))
                    lists.append(((net, 'excluded'), self.tree.get_children(gid)))
            for where, kids in lists:
                for i, did in enumerate(k for k in kids if k in devs):
                    d = nodes[did][1]
                    if self.tree.item(did, 'open'):
                        state['devices'].add((net, d['key']))
                    if did in selected:
                        state['selected'].add((net, d['key']))
                        state['gone_at'].setdefault(where, i)    # where to select if it disappears
        return state

    def _visible_rows(self):
        """Rows as displayed, top to bottom (children of closed rows left out)."""
        out, todo = [], list(reversed(self.tree.get_children()))
        while todo:
            iid = todo.pop()
            out.append(iid)
            if self.tree.item(iid, 'open'):
                todo.extend(reversed(self.tree.get_children(iid)))
        return out

    def _row_id(self, iid):
        net, d = getattr(self, 'inv_nodes', {}).get(iid, (None, None))
        return (net, d['key'] if d else None, self.tree.item(iid, 'text'))

    def fill_inventory(self):
        q = self.search.get().strip().lower()
        state = self._tree_state()
        same_view = state['q'] == q and (state['nets'] or state['devices'])
        self._last_q = q
        self.tree.delete(*self.tree.get_children())
        self.inv_nodes = {}                 # tree item -> (network, device)
        self.dev_rows, self.group_rows = set(), {}      # device rows; 'Excluded' rows -> network
        shown, reselect = 0, []

        def add_device(parent, net, d, excluded=False):
            parts = self._parts(d)
            if q:
                hay = ' '.join([display_name(d), d['mgmt_ip'], d['serial'], d['model'],
                                d['location'], d['version']] +
                               [' '.join(p[1]) + ' ' + p[0] for p in parts]).lower()
                if q not in hay:
                    return None
            tags = (['device'] + (['warn'] if d['warnings'] and not excluded else []) +
                    (['stale'] if d.get('stale') else []) + (['excluded'] if excluded else []))
            version = d['version'] + (' (%s)' % d['install_mode'] if d['install_mode'] else '')
            notes = ('Excluded by rule %s' % d.get('excluded_by')) if excluded else '; '.join(d['warnings'])
            was_open = (net, d['key']) in state['devices'] if same_view else bool(q) and not excluded
            did = self.tree.insert(parent, 'end', text=display_name(d), tags=tuple(tags), open=was_open,
                                   values=(device_kind(d), d['model'], d['serial'], '', version, '',
                                           d['mgmt_ip'], d['location'], notes))
            self.inv_nodes[did] = (net, d)
            self.dev_rows.add(did)
            if (net, d['key']) in state['selected']:
                reselect.append(did)
            for unit, vals in parts:
                self.inv_nodes[self.tree.insert(did, 'end', text=unit, values=vals,
                                                tags=('excluded',) if excluded else ())] = (net, d)
            return did
        for net in self.selected_networks():
            snap = self.snaps.get(net)
            if not snap:
                continue
            devices = [d for d in snap['devices'] if in_inventory(d)]
            excl = len(snap.get('excluded', []))
            nid = self.tree.insert('', 'end', text=net, open=state['nets'].get(net, True) if same_view else True,
                                   tags=('network',),
                                   values=('%d devices%s' % (len(devices), (', %d excluded' % excl) if excl else ''),))
            self.inv_nodes[nid] = (net, None)
            for d in sorted(devices, key=display_name):
                if add_device(nid, net, d):
                    shown += 1
            hidden = sorted(snap.get('excluded', []), key=display_name)
            if hidden:
                gid = self.tree.insert(nid, 'end', text='Excluded (%d)' % len(hidden), tags=('group',),
                                       open=state['nets'].get((net, 'excluded'), False) if same_view else False,
                                       values=('not counted', '', '', '', '', '', '', '',
                                               'Left out by exclusion rules - select a device here and '
                                               'click "Include selected device" to bring it back'))
                self.inv_nodes[gid] = (net, None)
                self.group_rows[gid] = net
                for d in hidden:
                    add_device(gid, net, d, excluded=True)
                if not self.tree.get_children(gid):       # nothing excluded matches the search
                    self.tree.delete(gid)
                    del self.group_rows[gid]
        self.inv_count.config(text='%d device%s shown' % (shown, '' if shown == 1 else 's'))
        self._autosize(self.tree, names=('#0',))
        if same_view:
            if not reselect:                  # the selected device was forgotten / (ex/in)cluded:
                for nid in self.tree.get_children():      # pick the one now in its place
                    net = self.inv_nodes[nid][0]
                    lists = [(net, self.tree.get_children(nid))] + [
                        ((net, 'excluded'), self.tree.get_children(g)) for g in self.tree.get_children(nid)
                        if g in self.group_rows]
                    for where, kids in lists:
                        kids = [k for k in kids if k in self.dev_rows]
                        i = state['gone_at'].get(where)
                        if i is not None and kids:
                            reselect.append(kids[min(i, len(kids) - 1)])
            if reselect:
                self.tree.selection_set(reselect)
            rows = self._visible_rows()           # same row at the top as before
            ids = [self._row_id(i) for i in rows]
            at = ids.index(state['top']) if state['top'] in ids else state['top_index']
            if rows:
                self.tree.update_idletasks()
                self.tree.yview_moveto(min(at, len(rows) - 1) / float(len(rows)))
        self._update_exclude_button()

    def _parts(self, d):
        out = []
        for unit, kind, it, uplinks in device_parts(d):
            if uplinks is not None:
                out.append((unit, (kind, it['pid'], it['serial'], ', '.join(u['pid'] for u in uplinks) or 'none',
                                   it['sw'], it['status'], '', '', '')))
            else:
                note = it.get('notes') or it['descr']
                if it.get('gaps'):
                    note += ' (came back %dx)' % it['gaps']
                if it.get('shared_serial'):
                    note += ' (shared / default serial, acknowledged)'
                out.append((unit, (kind, it['pid'], it['serial'], '', it['sw'],
                                   ' / '.join(x for x in (it['status'], it['role']) if x), '',
                                   it.get('location', ''), note)))
        return out

    def _expand(self, state):
        """Expand all: networks and devices open. Collapse all: one line per network."""
        for nid in self.tree.get_children():
            self.tree.item(nid, open=state)
            for did in self.tree.get_children(nid):
                self.tree.item(did, open=state)

    def fill_counts(self):
        """Columns: Kind, PID, Description, Total, then one per selected network (excluded
        devices are not counted - as in the inventory workbook)."""
        nets = [n for n in self.selected_networks() if self.snaps.get(n)]
        heads = ('Kind', 'PID', 'Description', 'Total') + tuple(nets)
        tree = self.counts
        tree.delete(*tree.get_children())
        ids = tuple('c%d' % i for i in range(len(heads)))     # network names may contain spaces
        tree.configure(columns=ids)
        for i, (h, w) in enumerate(zip(heads, (150, 200, 380, 70) + (90,) * (len(heads) - 4))):
            tree.heading(ids[i], text=h)
            tree.column(ids[i], width=self.px(w), anchor='w' if i < 3 else 'e')
        for r in part_counts([(n, self.snaps[n]['devices']) for n in nets]):
            tree.insert('', 'end', values=(KIND_LABELS.get(r['kind'], r['kind']), r['pid'], r['descr'],
                                           r['total']) + tuple(r['counts'].get(n, 0) for n in nets))
        note = excluded_note([(n, self.snaps[n].get('excluded', [])) for n in nets])
        if note:
            tree.note.config(text=note + ' They are on the Inventory tab, in each network\'s '
                                         '"Excluded" branch, and in the Excluded devices workbook.')
            tree.note.grid()
        else:
            tree.note.grid_remove()
        self._autosize(tree, stretch='c2')                    # Description takes the spare width

    def fill_issues(self):
        self.issues.delete(*self.issues.get_children())
        for net in self.selected_networks():
            for d in sorted(self.snaps.get(net, {}).get('devices', []), key=display_name):
                for w in d['warnings']:
                    self.issues.insert('', 'end', values=(net, display_name(d), w))
        self._autosize(self.issues, names=('Device',))

    def _changes_for(self, nets):
        """{network: (compared-with date, [(change, host, detail)])}"""
        mode = 'previous' if self.compare.get() == self.COMPARE[1] else 'round'
        out = {}
        for net in nets:
            i = self.ws.baseline_index(net, mode)
            if i is None or net not in self.snaps:
                out[net] = ('', [])
                continue
            if not hasattr(self, '_cache_used'):
                self._cache_used = set()
            old = self._load(net, i)
            out[net] = (old['created'][:16], diff_snapshots(old, self.snaps[net]))
        return out

    def fill_changes(self):
        self.changes.delete(*self.changes.get_children())
        changes = self._changes_for(self.selected_networks())
        for net, (since, rows) in [(n, changes[n]) for n in self.selected_networks()]:
            if not since:
                self.changes.insert('', 'end', values=(net, '', '(first import)', '', 'Nothing to compare yet'))
                continue
            for change, host, detail in rows or [('(no changes)', '', '')]:
                self.changes.insert('', 'end', values=(net, since, change, host, detail))
        self._autosize(self.changes, names=('Device',))

    # ------------------------------------------------------------------ actions
    def set_status(self, text):
        self.status.config(text=text)

    def _run(self, label, work, done=None):
        """Run work() in a thread; done(result) back on the GUI thread."""
        self.set_status(label + '...')

        def target():
            try:
                self.msgs.put(('ok', done, work()))
            except Exception:
                self.msgs.put(('err', label, traceback.format_exc()))
        threading.Thread(target=target, daemon=True).start()

    def _pump(self):
        try:
            while True:
                kind, a, b = self.msgs.get_nowait()
                if kind == 'ok':
                    if a:
                        a(b)
                elif kind == 'status':
                    self.set_status(a)
                else:
                    self.set_status('%s failed' % a)
                    messagebox.showerror(a, b[-2000:])
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def do_import(self):
        dlg = ImportDialog(self.root, sorted(self.snaps))
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        name, path, nodes = dlg.result

        def progress(n, p):
            if n % 50 == 0:
                self.msgs.put(('status', 'Reading files... %d' % n, None))

        def done(snap):
            self.refresh_networks(select=safe_name(name))
            self.set_status('Imported %s: %d devices (%d not reached - last known hardware kept; %d listed '
                            'under two names - counted once, see Issues), %d issues'
                            % (name, sum(1 for d in snap['devices'] if in_inventory(d)),
                               sum(1 for d in snap['devices'] if d.get('stale')),
                               snap['build_stats'].get('combined', 0),
                               sum(len(d['warnings']) for d in snap['devices'])))
        self._run('Importing %s' % name,
                  lambda: self.ws.import_exports(safe_name(name), path, nodes, progress), done)

    def do_import_manual(self):
        dlg = ManualDialog(self.root, sorted(self.snaps))
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        path, name = dlg.result

        def done(result):
            snaps, info = result
            self.refresh_networks(select=sorted(snaps)[0] if len(snaps) == 1 else None)
            skipped = '; '.join('row %d: %s' % p for p in info['problems'][:5])
            self.set_status('Manual sheet: %d rows used of %d -> %s%s'
                            % (info['used'], info['rows'], ', '.join(sorted(snaps)) or 'no networks',
                               ('  (' + skipped + ')') if skipped else ''))
        self._run('Importing manual sheet', lambda: self.ws.import_manual(path, safe_name(name) if name else ''), done)

    def do_manual_sheet(self, with_rows):
        nets = [n for n in self.selected_networks() if n in self.snaps] if with_rows else []
        path = filedialog.asksaveasfilename(defaultextension='.xlsx', filetypes=[('Excel', '*.xlsx')],
                                            initialfile='manual_inventory.xlsx' if not nets
                                            else 'manual_inventory_%s.xlsx' % '_'.join(nets[:3]),
                                            **_start())
        if not path:
            return
        write_xlsx(path, template_sheets([(n, self.ws.load(n, raw=True)['devices']) for n in nets]))
        self.set_status('Wrote ' + path)
        open_path(path)

    def do_optics(self):
        settings = self.ws.settings()
        settings['include_optics'] = bool(self.optics.get())
        self.ws.save_settings(settings)
        self.refresh_networks()
        self.set_status('Optics %s' % ('listed and counted' if self.optics.get() else 'hidden'))

    def do_forget(self):
        picked = {}
        for iid in self.tree.selection():
            net, d = self.inv_nodes.get(iid, (None, None))
            if d is not None:
                picked.setdefault(net, {})[d['key']] = display_name(d)
        if not picked:
            messagebox.showinfo('Forget device', 'Select the device(s) to forget on the Inventory tab first.')
            return
        names = ', '.join('%s [%s]' % (h, n) for n in sorted(picked) for h in sorted(picked[n].values()))
        if not messagebox.askyesno('Forget device', (
                'Remove %s from the inventory?\n\nUse this for devices that are gone (decommissioned, '
                'replaced) and are only still listed as "not reached". A new snapshot is saved; '
                'Changes will list them as missing. Devices from the manual sheet are not affected - '
                'take them off the sheet instead.') % names):
            return
        done = []
        for net, keys in picked.items():
            done += self.ws.forget(net, list(keys))
        self.refresh_networks()
        self.set_status('Forgot %s' % (_some(done) or 'nothing (manual devices: edit the sheet)'))

    def _picked_devices(self):
        """[(network, device)] for the selected rows (a part row counts as its device)."""
        out, seen = [], set()
        for iid in self.tree.selection():
            net, d = self.inv_nodes.get(iid, (None, None))
            if d is not None and (net, d['key']) not in seen:
                seen.add((net, d['key']))
                out.append((net, d))
        return out

    def _update_exclude_button(self):
        picked = self._picked_devices()
        excluded = [bool(d.get('excluded_by')) for _, d in picked]
        self.excl_button.config(text='Exclude / include selected' if picked and any(excluded) and not all(excluded)
                                else 'Include selected device' if picked and all(excluded)
                                else 'Exclude selected device')
        serials = self._picked_serials()
        unmark = bool(serials) and set(serials) <= set(self.ws.shared_serials())
        self.shared_button.config(text='Unmark shared serial' if unmark else 'Mark serial as shared/default')

    def _picked_serials(self):
        """Serials of the selected part rows; a selected device row gives the serials its
        'default serial shared by two parts' issues name."""
        out = []
        for iid in self.tree.selection():
            net, d = self.inv_nodes.get(iid, (None, None))
            if d is None:
                continue
            if iid in self.dev_rows:
                found = [m.group(1) for m in (SHARED_WARNING.match(w) for w in d['warnings']) if m]
            else:
                found = [self.tree.set(iid, 'serial')]
            out += [s for s in found if s and s not in out]
        return out

    def do_shared_serial(self):
        """Acknowledge the selected parts' serials as a factory default shared by different parts
        (or take the acknowledgement back): no more issue, each part counted separately."""
        serials = self._picked_serials()
        if not serials:
            messagebox.showinfo('Shared serial', 'Select the part (or a device whose Notes say "default '
                                'serial shared by two parts") on the Inventory tab first.')
            return
        known = self.ws.shared_serials()
        if set(serials) <= set(known):
            self.ws.set_shared_serials([s for s in known if s not in serials])
            self.refresh_networks()
            self.set_status('No longer treated as shared: %s' % _some(serials))
            return
        if not messagebox.askyesno('Shared serial', 'Treat %s as a factory default serial shared by '
                                   'different parts?\n\nThe duplicate-serial issue goes away and every '
                                   'module / PSU / fan reporting it is counted as its own part. It is '
                                   'listed on the workbook\'s About sheet.' % ', '.join(serials)):
            return
        self.ws.set_shared_serials(known + serials)
        self.refresh_networks()
        self.set_status('Shared / default serial: %s  - select it again to unmark' % _some(serials))

    def do_exclude_selected(self):
        """Exclude the selected devices - or, for devices in an 'Excluded' branch, bring them back."""
        picked = self._picked_devices()
        if not picked:
            messagebox.showinfo('Exclude', 'Select the device(s) on the Inventory tab first.')
            return
        done = {'excluded': [], 'included': []}
        for net in sorted(set(n for n, _ in picked)):
            out = [display_name(d) for n, d in picked if n == net and not d.get('excluded_by')]
            back = [d for n, d in picked if n == net and d.get('excluded_by')]
            if out:
                self.ws.set_exclusions(net, self.ws.exclusions(net) + out)
            if back:
                self.ws.include(net, back)
            done['excluded'] += out
            done['included'] += [display_name(d) for d in back]
        self.refresh_networks()
        self.set_status('; '.join('%s %s' % (k.capitalize(), _some(v)) for k, v in done.items() if v)
                        + ('  - to undo, select them in the network\'s "Excluded" branch' if done['excluded'] else ''))

    def _tree_double(self, event):
        """Double-click on an Unrecognised part: open the rules with its PID filled in."""
        iid = self.tree.identify_row(event.y)
        if not iid or iid in self.dev_rows or iid not in self.inv_nodes:
            return None
        if self.tree.set(iid, 'kind') != KIND_LABELS['other']:
            return None
        self.do_rules(self.tree.set(iid, 'model'))
        return 'break'

    def do_rules(self, pid=''):
        for w in self.root.winfo_children():
            if isinstance(w, RulesDialog) and w.winfo_exists():
                w.lift()                         # already open (e.g. a triple-click)
                return
        RulesDialog(self.root, self, pid)

    def do_exclusion_rules(self):
        net = self._current() or (self.selected_networks() or [None])[0]
        if not net:
            messagebox.showinfo('Exclusion rules', 'Click a network name on the left first.')
            return
        ExclusionDialog(self.root, self, net)

    def do_save_keep(self):
        text = self.keep_days.get().strip()
        if text and not text.isdigit():
            messagebox.showwarning('Settings', 'Enter a number of days, or leave it blank for forever.')
            return
        settings = self.ws.settings()
        settings['keep_unreached_days'] = int(text) if text else None
        self.ws.save_settings(settings)
        self.set_status('Unreached devices: last known hardware kept %s (applies from the next import)'
                        % ('for %s days' % text if text else 'until forgotten'))

    def do_import_snapshot(self):
        path = filedialog.askopenfilename(filetypes=[('NetInv snapshot', '*.json'), ('All files', '*.*')],
                                          **_start())
        if path:
            try:
                name = self.ws.import_snapshot_file(path)
                self.refresh_networks(select=safe_name(name))
                self.set_status('Added snapshot for %s' % name)
            except Exception as exc:
                messagebox.showerror('Import snapshot', str(exc))

    def do_export_snapshot(self):
        nets = [self._current()] if self._current() else []
        if len(nets) != 1:
            messagebox.showinfo('Export snapshot', 'Click a network name on the left first.')
            return
        folder = filedialog.askdirectory(title='Where to put the transfer file')
        if folder:
            src = self.ws.snapshots(nets[0])[-1]
            for f in (src, src + '.sha256'):
                if os.path.exists(f):
                    shutil.copy2(f, os.path.join(folder, '%s_%s' % (safe_name(nets[0]), os.path.basename(f))))
            self.set_status('Copied %s snapshot to %s' % (nets[0], folder))

    def do_remove(self):
        nets = [self._current()] if self._current() else []
        if not nets:
            messagebox.showinfo('Remove network', 'Click a network name on the left first.')
        elif messagebox.askyesno('Remove', 'Remove %s and all its snapshots?' % ', '.join(nets)):
            for n in nets:
                self.ws.remove(n)
            self.refresh_networks()

    def _outdir(self):
        d = self.out_dir.get()
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    def _open_out(self):
        open_path(self._outdir())

    def do_workbook(self):
        nets = [n for n in self.selected_networks() if n in self.snaps]
        if not nets:
            return
        path = os.path.join(self._outdir(), 'inventory_%s.xlsx' % ('_'.join(nets) if len(nets) <= 3 else 'all'))
        data = [(n, self.snaps[n]['devices']) for n in nets]
        settings = self.ws.settings()
        about = about_sheet([(n, self.snaps[n]) for n in nets], self.ws.include_optics(),
                            shared_serials=self.ws.shared_serials(),
                            kind_rules=settings.get('kind_rules'), role_rules=settings.get('role_rules'))
        compared = self._changes_for(nets)       # baselines come from memory (_load)
        changes = dict((n, rows) for n, (_, rows) in compared.items())
        baselines = dict((n, since) for n, (since, _) in compared.items())

        def work():
            write_xlsx(path, workbook_for_networks(data, changes, baselines, about))
            return path

        def done(p):
            self.set_status('Wrote ' + p)
            open_path(p)
        self._run('Writing workbook', work, done)

    def do_excluded_workbook(self):
        nets = [n for n in self.selected_networks() if self.snaps[n].get('excluded')]
        if not nets:
            messagebox.showinfo('Excluded devices', 'Nothing is excluded in the ticked networks.')
            return
        path = os.path.join(self._outdir(), 'excluded_%s.xlsx' % ('_'.join(nets) if len(nets) <= 3 else 'all'))

        def work():
            write_xlsx(path, excluded_workbook([(n, self.snaps[n]['excluded']) for n in nets]))
            return path

        def done(p):
            self.set_status('Wrote ' + p)
            open_path(p)
        self._run('Writing excluded-devices workbook', work, done)

    def do_summary(self):
        nets = [n for n in self.selected_networks() if n in self.snaps]
        texts = []
        for n in nets:
            s = self.snaps[n]
            texts.append(feasibility_report([('Network', '(name withheld)'), ('Tool', VERSION)],
                                            s['split_stats'], s['build_stats'], s['devices'],
                                            s.get('nodes_info'), []))
        path = os.path.join(self._outdir(), 'sanitized_summary.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('\n\n'.join(texts))
        self.set_status('Wrote ' + path)
        open_path(path)

    def _selected_commands(self):
        return [c for c, v in self.cmd_vars if v.get()]

    def _refresh_script(self):
        self.script.delete('1.0', 'end')
        self.script.insert('1.0', script_text(self._selected_commands()))

    def _copy_script(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(script_text(self._selected_commands()))
        self.set_status('Copied %d commands to the clipboard' % len(self._selected_commands()))

    def _save_script(self):
        path = filedialog.asksaveasfilename(defaultextension='.txt', initialfile='netinv_ncm_commands.txt',
                                            filetypes=[('Text', '*.txt')])
        if path:
            with open(path, 'w') as fh:
                fh.write(script_text(self._selected_commands()))
            self.set_status('Saved ' + path)


def run(workspace_dir, input_dir=None):
    global INPUT_DIR
    INPUT_DIR = input_dir
    try:                                    # crisp text on scaled Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root, workspace_dir)
    root.mainloop()


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), 'inventory_data'))
