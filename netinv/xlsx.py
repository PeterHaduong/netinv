"""Minimal .xlsx writer and reader (standard library only).

Writes sheets of text cells with a bold, frozen, auto-filtered header row and
sensible column widths. Optional per-row styling (bold / fill colour) and thick
outlines around groups of rows ("blocks", e.g. one chassis and its modules).
Python ints and floats are stored as numbers (so counts can be summed); every other
value is stored as text, so Excel never turns serial numbers or versions like '17.9'
into numbers or dates.

    write_xlsx(path, [(sheet_name, header, rows),
                      (sheet_name, header, rows, {'row_styles': {0: {'bold': True, 'fill': 'DDEBF7'}},
                                                  'blocks': [(0, 3), (4, 9)]})])
Row indexes in options are 0-based data rows (the header is not counted).

read_xlsx(path) reads back what Excel saves (shared strings, inline strings, numbers):
[(sheet_name, [[cell text, ...], ...])], for the manual inventory sheet.
"""

import re
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
HEADER_FILL = 'DDEBF7'


def _esc(value):
    return escape(_ILLEGAL.sub('', '' if value is None else str(value)), {'"': '&quot;'})


def _col(n):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_name(name, used):
    base = re.sub(r'[\[\]:*?/\\]', '_', name)[:31] or 'Sheet'
    out, i = base, 2
    while out.lower() in used:
        out = '%s (%d)' % (base[:26], i)
        i += 1
    used.add(out.lower())
    return out


class _Styles(object):
    """Registry of cell formats: (bold, fill, top, bottom, left, right, text, wrap, valign_top)
    -> cellXfs index. text = Excel's '@' (Text) number format, so typed serials stay exactly as
    typed; wrap = Wrap Text; valign_top = vertical alignment Top."""

    def __init__(self):
        self.fills = ['none', 'gray125']
        self.borders = [(None, None, None, None)]
        self.xfs = [(0, 0, 0, 0, False, False)]    # (font, fill, border, numFmt, wrap, top)
        self._index = {(False, None, None, None, None, None, False, False, False): 0}

    def get(self, bold=False, fill=None, top=None, bottom=None, left=None, right=None, text=False,
            wrap=False, valign_top=False):
        key = (bool(bold), fill, top, bottom, left, right, bool(text), bool(wrap), bool(valign_top))
        if key in self._index:
            return self._index[key]
        if fill and fill not in self.fills:
            self.fills.append(fill)
        border = (left, right, top, bottom)
        if border not in self.borders:
            self.borders.append(border)
        self.xfs.append((1 if bold else 0, self.fills.index(fill) if fill else 0,
                         self.borders.index(border), 49 if text else 0, bool(wrap), bool(valign_top)))
        self._index[key] = len(self.xfs) - 1
        return self._index[key]

    def xml(self):
        fills = ''.join('<fill><patternFill patternType="%s"/></fill>' % f if f in ('none', 'gray125')
                        else '<fill><patternFill patternType="solid"><fgColor rgb="FF%s"/></patternFill></fill>' % f
                        for f in self.fills)

        def edge(tag, style):
            return ('<%s style="%s"><color rgb="FF404040"/></%s>' % (tag, style, tag)) if style else '<%s/>' % tag
        borders = ''.join('<border>%s%s%s%s<diagonal/></border>'
                          % (edge('left', l), edge('right', r), edge('top', t), edge('bottom', b))
                          for l, r, t, b in self.borders)
        def xf(fo, fi, bo, nf, wrap, vtop):
            align = ''.join(a for a, on in ((' vertical="top"', vtop), (' wrapText="1"', wrap)) if on)
            return ('<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0"%s%s%s%s%s>%s</xf>'
                    % (nf, fo, fi, bo, ' applyFont="1"' if fo else '', ' applyFill="1"' if fi else '',
                       ' applyBorder="1"' if bo else '', ' applyNumberFormat="1"' if nf else '',
                       ' applyAlignment="1"' if align else '',
                       ('<alignment%s/>' % align) if align else ''))
        xfs = ''.join(xf(*x) for x in self.xfs)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                '<fills count="%d">%s</fills><borders count="%d">%s</borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="%d">%s</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                '</styleSheet>' % (len(self.fills), fills, len(self.borders), borders, len(self.xfs), xfs))


def _sheet_xml(header, rows, opts, styles):
    ncol = max([len(header)] + [len(r) for r in rows]) if rows else len(header)
    widths = [len(str(h)) for h in header] + [0] * (ncol - len(header))
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], min(50, len(str(v if v is not None else ''))))
    wrap = opts.get('wrap') or {}
    for i, w in wrap.items():            # long text: up to its own width, wrapped beyond that
        if i < ncol:
            longest = max([len(str(r[i])) for r in rows if i < len(r) and r[i] is not None] or [0])
            widths[i] = max(min(longest, w), len(str(header[i])) if i < len(header) else 0)
    table = opts.get('table')
    text = bool(opts.get('text_columns'))
    row_styles = opts.get('row_styles', {})
    outline = opts.get('outline', {})
    edges = {}                           # (row, col) -> dict of thick edges
    for first, last in opts.get('blocks', []):
        for r in range(first, last + 1):
            for c in range(ncol):
                e = edges.setdefault((r, c), {})
                if r == first:
                    e['top'] = 'medium'
                if r == last:
                    e['bottom'] = 'medium'
                if c == 0:
                    e['left'] = 'medium'
                if c == ncol - 1:
                    e['right'] = 'medium'

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
    if outline:
        # Group summary (the device row) sits above its detail rows.
        out.append('<sheetPr><outlinePr summaryBelow="0"/></sheetPr>')
    out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
               'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"%s/><cols>'
               % (' outlineLevelRow="%d"' % max(outline.values()) if outline else ''))
    col_style = (' style="%d"' % styles.get(text=True)) if text else ''
    for i, w in enumerate(widths):
        out.append('<col min="%d" max="%d" width="%d" customWidth="1"%s/>' % (i + 1, i + 1, w + 3, col_style))
    out.append('</cols><sheetData>')
    head_style = 0 if table else styles.get(bold=True, fill=HEADER_FILL)
    out.append('<row r="1">')
    for c, v in enumerate(header):
        out.append('<c r="%s1" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>'
                   % (_col(c), (' s="%d"' % head_style) if head_style else '', _esc(v)))
    out.append('</row>')
    for r, row in enumerate(rows):
        rs = row_styles.get(r, {})
        level = outline.get(r, 0)
        out.append('<row r="%d"%s>' % (r + 2, (' outlineLevel="%d"' % level) if level else ''))
        for c in range(ncol):
            v = row[c] if c < len(row) else ''
            e = edges.get((r, c), {})
            s = (styles.get(bold=rs.get('bold'), fill=rs.get('fill'), text=text, wrap=c in wrap,
                            valign_top=bool(wrap), **e) if (rs or e or text or wrap) else 0)
            if v is None or v == '':
                if s:
                    out.append('<c r="%s%d" s="%d"/>' % (_col(c), r + 2, s))
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append('<c r="%s%d"%s><v>%s</v></c>' % (_col(c), r + 2, (' s="%d"' % s) if s else '', v))
                continue
            out.append('<c r="%s%d" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>'
                       % (_col(c), r + 2, (' s="%d"' % s) if s else '', _esc(v)))
        out.append('</row>')
    out.append('</sheetData>')
    ref = 'A1:%s%d' % (_col(max(len(header), 1) - 1), max(len(rows), 1) + 1)
    lists = opts.get('lists') or {}
    if lists:                            # drop-down choices for whole columns
        out.append('<dataValidations count="%d">' % len(lists))
        for c, values in sorted(lists.items()):
            out.append('<dataValidation type="list" allowBlank="1" showErrorMessage="0" sqref="%s2:%s%d">'
                       '<formula1>"%s"</formula1></dataValidation>'
                       % (_col(c), _col(c), opts.get('list_rows', 5000), _esc(','.join(values))))
        out.append('</dataValidations>')
    if table:
        out.append('<tableParts count="1"><tablePart r:id="rId1"/></tableParts>')
    elif opts.get('autofilter', True):
        out.append('<autoFilter ref="%s"/>' % ref)
    out.append('</worksheet>')
    return ''.join(out)


def _table_xml(index, name, header, rows):
    """An Excel Table (Insert > Table): banded rows, sort/filter buttons, sorts as one unit."""
    ref = 'A1:%s%d' % (_col(len(header) - 1), max(len(rows), 1) + 1)
    seen, cols = set(), []
    for h in header:                     # table column names must be unique and non-empty
        h = str(h) or 'Column'
        base, n = h, 2
        while h.lower() in seen:
            h = '%s %d' % (base, n)
            n += 1
        seen.add(h.lower())
        cols.append(h)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="%d" '
            'name="%s" displayName="%s" ref="%s" totalsRowShown="0"><autoFilter ref="%s"/>'
            '<tableColumns count="%d">%s</tableColumns>'
            '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" '
            'showRowStripes="1" showColumnStripes="0"/></table>'
            % (index, name, name, ref, ref, len(cols),
               ''.join('<tableColumn id="%d" name="%s"/>' % (i + 1, _esc(c)) for i, c in enumerate(cols))))


def write_xlsx(path, sheets):
    """sheets: list of (name, header, rows) or (name, header, rows, options).

    options: 'table': 'TableName' (make the sheet an Excel Table), 'row_styles', 'blocks',
    'outline': {row: level} (collapsible row groups), 'autofilter': False,
    'text_columns': True (every column in Text format), 'lists': {column index: [choices]},
    'wrap': {column index: width} (fixed width + Wrap Text for long text; the sheet's cells are
    then top-aligned and Excel sizes each row to its wrapped text)."""
    used = set()
    names = [_sheet_name(s[0], used) for s in sheets]
    opts = [s[3] if len(s) > 3 else {} for s in sheets]
    styles = _Styles()
    bodies = [_sheet_xml(s[1], s[2], o, styles) for s, o in zip(sheets, opts)]

    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    defined, tables = [], []
    for i, name in enumerate(names, 1):
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/'
                  'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i)
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(name), i, i))
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                    % (i, i))
        header, rows, o = sheets[i - 1][1], sheets[i - 1][2], opts[i - 1]
        if o.get('table'):
            tables.append((i, len(tables) + 1, re.sub(r'\W', '_', o['table']), header, rows))
        elif o.get('autofilter', True):
            defined.append('<definedName name="_xlnm._FilterDatabase" localSheetId="%d" hidden="1">'
                           "'%s'!$A$1:$%s$%d</definedName>"
                           % (i - 1, _esc(name.replace("'", "''")), _col(max(len(header), 1) - 1),
                              len(rows) + 1))
    for _, n, _, _, _ in tables:
        ct.append('<Override PartName="/xl/tables/table%d.xml" ContentType="application/'
                  'vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>' % n)
    ct.append('</Types>')
    wb.append('</sheets>%s</workbook>' % ('<definedNames>%s</definedNames>' % ''.join(defined)
                                           if defined else ''))
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/styles" Target="styles.xml"/></Relationships>' % (len(names) + 1))
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ''.join(ct))
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', ''.join(wb))
        z.writestr('xl/_rels/workbook.xml.rels', ''.join(rels))
        z.writestr('xl/styles.xml', styles.xml())
        for i, body in enumerate(bodies, 1):
            z.writestr('xl/worksheets/sheet%d.xml' % i, body)
        for sheet_no, n, name, header, rows in tables:
            z.writestr('xl/tables/table%d.xml' % n, _table_xml(n, name, header, rows))
            z.writestr('xl/worksheets/_rels/sheet%d.xml.rels' % sheet_no,
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                       '2006/relationships/table" Target="../tables/table%d.xml"/></Relationships>' % n)


# --------------------------------------------------------------------------- reader

_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _col_index(ref):
    letters = re.match(r'[A-Z]+', ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(el):
    """All <t> text under an element (rich-text runs included)."""
    return ''.join(t.text or '' for t in el.iter(_NS + 't'))


def read_xlsx(path):
    """[(sheet name, rows)] with every cell as text. Whole numbers come back without '.0'."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if 'xl/sharedStrings.xml' in names:
            shared = [_text(si) for si in ET.fromstring(z.read('xl/sharedStrings.xml')).iter(_NS + 'si')]
        rels = {}
        for rel in ET.fromstring(z.read('xl/_rels/workbook.xml.rels')):
            target = rel.get('Target')
            rels[rel.get('Id')] = (target.lstrip('/') if target.startswith('/')
                                   else posixpath.normpath(posixpath.join('xl', target)))
        out = []
        for sheet in ET.fromstring(z.read('xl/workbook.xml')).iter(_NS + 'sheet'):
            part = rels.get(sheet.get(_REL_NS + 'id'))
            if part not in names:
                continue
            rows = []
            for row in ET.fromstring(z.read(part)).iter(_NS + 'row'):
                cells = {}
                for i, c in enumerate(row.findall(_NS + 'c')):
                    kind, v = c.get('t'), c.find(_NS + 'v')
                    if kind == 'inlineStr':
                        val = _text(c)
                    elif v is None or v.text is None:
                        val = ''
                    elif kind == 's':
                        val = shared[int(v.text)]
                    else:
                        val = v.text
                        if kind is None and re.match(r'^-?\d+\.0+$', val):
                            val = val.split('.')[0]
                    cells[_col_index(c.get('r')) if c.get('r') else i] = val
                number = int(row.get('r') or len(rows) + 1)
                while len(rows) < number - 1:          # keep Excel's row numbers (blank rows)
                    rows.append([])
                rows.append([cells.get(i, '') for i in range(max(cells) + 1)] if cells else [])
            out.append((sheet.get('name'), rows))
    return out
