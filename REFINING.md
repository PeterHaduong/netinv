# Refining NetInv against real output with a chat assistant

The code here is built and tested against synthetic data. Real exports will differ in places.
This guide covers fixing those differences where the real data lives, using a chat-only assistant,
with `python -m unittest` doing the back-testing so you don't have to retest by hand.

## The loop

1. **Run on real data.** `python tools/debug_parse.py <exports folder>` shows, per device, what was
   extracted: fields, hardware rows, parts left out for having no serial number, warnings,
   and any UNRECOGNISED part numbers. Add `--device HOST --raw` to see one device's raw text next to the result.
2. **Find what's wrong.** Common cases: a field is empty, a part is the wrong kind, a device is
   split in two or named after a file, or a part was left out because its serial was not read.
3. **Ask the assistant** using a template below. Paste **one module** plus the raw snippet and the
   debug output. Each module is small enough to paste whole:

   | Problem | Paste this file |
   |---|---|
   | Hostname convention / Role column | `netinv/roles.py` |
   | Part is the wrong kind / "Unrecognised" | `netinv/kinds.py` |
   | A serial placeholder not recognised (part kept or dropped wrongly) | `netinv/textutil.py` (`real_serial`) |
   | A show command's fields are wrong or empty | `netinv/parsers.py` (just the one function is fine) |
   | Devices split or named after a file; a command not recognised | `netinv/splitter.py` |
   | Stacks, VDCs, FEX, the no-serial rule, part counts | `netinv/model.py` |
   | Manual sheet: columns, header names, kinds, stock | `netinv/manual.py` |
   | Unreached devices, first / last seen | `netinv/history.py` |
   | Commands in the SolarWinds list | `netinv/commands.py` |
   | Workbook layout | `netinv/report.py` |
   | App screens | `netinv/gui.py` |

4. **Apply the change, then lock it in** with a fixture:
   `python tools/debug_parse.py <exports folder> --save-fixture <short-name>`
   Open `tests/fixtures_local/<short-name>/expected.json`, correct anything that is still wrong
   (it records what the tool says *now*), and save.
5. **Back-test everything:** `python -m unittest discover -s tests`
   This runs every saved real-data fixture (plus the synthetic tests, where present), so a fix for one platform
   can't silently break another. Fix until everything passes.

`tests/fixtures_local/` holds real data, so it stays where it was collected. Nothing in the
tool sends data anywhere.

## Rules to give the assistant (paste once per session)

```
You are helping maintain a Python tool with these hard constraints:
- Python standard library ONLY. No pip packages. Must run on Python 3.6+.
  (no dataclasses, no walrus :=, no match statements, no f-string '=' specifier).
- Make the smallest change that fixes the problem; keep existing behaviour for other platforms.
- Parsers must be tolerant: extract what matches, never raise on unexpected text.
- Return the complete modified function(s), not the whole file, unless asked.
- If a regex changes, show 2-3 example lines it must match and 1 it must not.
```

## Templates

**A field is wrong or missing**
```
Cisco <platform/model> output. The tool extracts <field> as "<what it got>" but it should be
"<correct value>". Here is the raw output (sanitised) and the current function.
--- raw ---
<paste the relevant lines from debug_parse --raw>
--- function ---
<paste parse_xxx() from netinv/parsers.py>
Fix the function. Keep it working for IOS-XE, NX-OS and classic IOS.
```

**A part is unrecognised or the wrong kind**
```
These show inventory entries get the wrong kind. Expected kind is in brackets.
NAME: "...", DESCR: "...", PID: ...   [linecard]
Here is netinv/kinds.py. Add or adjust a rule (PID rules first, keyword rules last).
```

**Devices split, merged, or named after a file**
```
debug_parse shows these sections (file, command, device, how recognised):
<paste the SPLIT lines>
The real layout is: <describe, e.g. "one file per device named <IP>_<date>.txt, no prompts,
first line is '#### NODE: <name> ####'">. Here is netinv/splitter.py. Make it handle this
layout without breaking the prompt-echo and per-folder layouts.
```

**Hostname convention / roles / icons**
```
Our names look like <pattern examples>. Tokens: AS access, CS core, ... Here is
netinv/roles.py. Update ROLE_TOKENS / MODEL_RULES / LABEL accordingly.
```

## Sanitising snippets

If a snippet must leave its network (for example, to bring it home to me), replace hostnames,
IPs, serials and MACs consistently (HOST-A, 10.0.0.1, SERIAL1, ...). Keep the spacing and
punctuation exactly: parsers depend on column positions and separators. Only do this where
your handling rules allow. The app's Sanitized summary (Outputs tab) is already built to need no
sanitising.
