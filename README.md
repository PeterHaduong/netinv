# NetInv - network hardware inventory from SolarWinds output

Turns SolarWinds NCM show-command output, plus a hand-filled **manual inventory sheet** for
anything SolarWinds cannot collect, into an **Excel inventory workbook** (what the business uses).
Python standard library only, Python 3.6+. Fixing parsers against real output:
[REFINING.md](REFINING.md).

## Folders

| Folder / file | What it is |
|---|---|
| `main.py` | The app: double-click it. |
| `netinv/` | The program code. Nothing to change here to use the app. |
| `input/` | Put SolarWinds results and manual inventory sheets here; the app's file pickers open in it. |
| `inventory_data/` | Created by the app: every import (snapshots), your settings (ticked networks, order, exclusion rules) and `outputs/` (workbooks). **Back it up** - it is your history. |
| `tools/`, `tests/`, `REFINING.md` | Only for fixing parsers against real output. |

`input/` and `inventory_data/` hold real data and are never committed to git. (A data folder
from an earlier version, `netinv_data/`, is renamed to `inventory_data/` automatically.)

## 1. Collect

In the app, open **SolarWinds commands**, click **Copy to clipboard**, and paste the list into a
SolarWinds NCM *Execute Command Script* job. Run it against every device. It is one list for all
platforms: a device that rejects a command (for example `show module` on a C8300) is skipped
automatically. The list starts with `terminal length 0` and has a `!` line between commands,
so it can also be pasted into an SSH session by hand without commands (notably `show inventory`)
being lost. Save the job results as a text file. You can also use terminal captures or NCM
config-archive folders, in any mix. Output of commands NetInv no longer uses (CDP, routing) is
ignored, so results from an older job still work.

**Several SolarWinds instances / overlapping collections:** put all the result files in one
import (a folder, or several files), or import them one after the other into the same network.
Each device appears once: where two files cover the same node, the newest usable output of each
command is used, and a node one instance could not reach but the other could shows the good data
with no error. A node named differently in the two instances (`as1.example.com` vs `as1`, with or
without a note, or an IP address as its name) is matched only on hard evidence: the same show
version hostname plus an overlapping chassis / stack serial number or the same SolarWinds IP
address; an unreachable node joins only the device with its IP address. Two different boxes are
never merged, even when they share a hostname or a SolarWinds name. Every match is listed on
the Issues tab ("Also listed in SolarWinds as ... - counted once"), and the import summary says
how many there were. Domain names are dropped from hostnames.

## 2. Manual inventory (isolated networks, bench stock)

For hardware SolarWinds can never collect, fill in the **manual inventory sheet**: in the app,
**Outputs -> Blank manual sheet** (or use `examples/manual_inventory_template.xlsx`). One row per
part: Network, Location, Hostname, Unit, Kind, PID, Serial, Description, Status, Mgmt IP,
Software, Notes. The sheet's *How to fill* tab explains each column.

* Rows with the same **Hostname** are one device (`Switch 1`, `Switch 2`, `Switch 1 Uplink Module` ...).
* Rows with **no Hostname** are loose stock, grouped by Location (`STOCK - Cage 2 shelf B`); Status
  says Spare / RMA / Loaner / Failed. Optic rows are only shown when optics are switched on (section 5).
* One sheet can hold several networks (Network column), e.g. every site plus `BENCH`.
* **Serial is required**: rows without one are left out and listed on Issues with their row number.
* Import it with **Import manual sheet...**. Each network named on it gets the sheet's rows as its
  manual devices, replacing the previous ones; devices collected by SolarWinds in the same network
  are kept (a hostname in both uses the SolarWinds data). To change things later, edit the same
  file and import it again, or get the current rows back with **Outputs -> Manual sheet to edit**.
  Changes lists what moved, including stock status (Spare -> RMA).

## 3. Run the app (on the admin PC)

Double-click `main.py` (or run `python main.py`).

1. **Import SolarWinds output...**: pick the network name and the results file or folder. The
   hostname and management IP are taken from the SolarWinds headers. Node names with a note after
   an ambiguous hostname (`campus-as1 (BLDG 2)`) are matched to the `show version` hostname and
   shown with the note, so two nodes that are both `campus-as1` stay separate.
   **Import manual sheet...** for the manual inventory.
2. **Networks** (left): tick the networks to show and output (All / None; double-click a name to
   show only that one). Click a name to pick it for **Move up / Move down** (the order is used in
   the app and the workbook, e.g. the order you work through them), Export or Remove.
3. Check the tabs: **Inventory** (collapsible network -> device -> stack members / modules / FEX /
   PSUs, with search; grey = not reached this time; **Collapse all** leaves one line per network),
   **Part counts** (each unique part number and how many there are), **Issues**, **Changes**.
   **Exclusion rules...** / **Exclude selected device** hide another team's equipment, e.g. the
   phone shop's devices on a shared SolarWinds: hostname patterns per network (`PS-*`, `*-VG*`).
   Excluded devices stay visible, greyed, in an **Excluded (N)** branch under their network:
   select one there and the button becomes **Include selected device**. A device caught by a
   wildcard comes back through a keep rule (`!PS-CORE1`), so the rest stay excluded. Excluded
   devices are still imported and tracked, but appear nowhere in the inventory workbook.
   (Command line: `python main.py exclude NET-A "PS-*"`, `... "!PS-CORE1"`, `--remove`.)
4. **Outputs -> Excel workbook**: one grouped sheet per network (collapse with the 1/2 buttons),
   plus sortable Devices / Hardware / Serial numbers / Part counts / Issues / Changes tables.
   **Serial numbers** has one row per unique serial (what it is, which devices list it). Only your own
   equipment: excluded devices are in no sheet and no count. It opens with an **About** sheet:
   when it was made, the networks, how parts are counted (serial only, counted once, optics) and,
   when anything was excluded, what was left out and by which rules. The Part counts tab in the
   app shows the same "Not counted" note above the table.
   **Outputs -> Excluded devices workbook** (only when you need it): a separate file with just
   the excluded devices, same layout, every network marked "(excluded)", and a first sheet
   naming the rule that caught each device. It can be handed to the team that owns the gear.
   (Command line: `python main.py workbook excluded.xlsx --excluded`.)
5. **Export snapshot file...** copies a network's parsed data (plain JSON plus a SHA-256 file) for
   transfer to the central network. There, **Import snapshot file...** adds it.

Command line (no GUI): `python main.py import NET-A <results.txt>`,
`python main.py import-manual sheet.xlsx [--network NAME]`, `python main.py manual-sheet out.xlsx [NETWORK ...]`,
`python main.py forget NET-A HOSTNAME`, `python main.py workbook out.xlsx`, `python main.py commands`.

## 4. The yearly round, and devices SolarWinds does not reach

The inventory is meant to be taken about once a year: run the SolarWinds job, import it, then run
a **retry job** against the devices that failed and import that into the same network (as many
times as needed), and import the manual sheet. Imports less than 30 days apart are one round.

* A device collected earlier in the round stays current when a retry does not include it.
* A device **not reached at all this round** keeps its **last known hardware** (e.g. last year's),
  shown greyed with "not reached since <date>" (Devices sheet: Source and Last reached columns).
  Its parts still count. It stays until you remove it: select it on the Inventory tab and click
  **Forget selected device...** (for gear that is really gone). Optionally, Outputs -> Settings
  drops such devices automatically after a number of days.
* **Changes** compare with the **previous round** (last year's inventory), so retries do not hide
  the year's changes; the Changes tab can switch to the previous import. Changes says
  **Not reached** / **Reached again** instead of listing every part as removed / added.
* Every part has **First seen**, **Last seen** and **Seen** ("2 of 3 imports, came back 1x") on the
  Hardware sheet. A part only counts as missing when its device was reached and the part was not
  there, so hardware that really comes and goes stands out.

## 5. What is counted

* **Only hardware with a serial number** (SolarWinds and manual alike). A provisioned-but-absent
  stack member, an empty slot or a fan that reports `SN: N/A` is left out of every sheet and of
  the counts. Missing chassis, stack members, FEX and modules are listed on the Issues sheet so
  nothing disappears silently. A device with no serial at all (for example one SolarWinds has
  never been able to collect) appears only on Issues.
* **Optics (SFPs) are left out** by default. They are still read and stored, so ticking
  **Include optics** under Outputs -> Settings lists and counts them (from devices and from the
  manual sheet) without re-importing.
* **One serial = one part.** A serial listed twice inside one device (a stack or DAC cable seen
  from both ends, SMART fans sharing a serial) is one part; rows that are part of the chassis
  itself (an N9K "Slot 1", a FEX's own module) are not listed separately.
* **Part counts** has one row per PID with a Total and a column per network. Each physical part
  is counted once by serial number: a dual-homed FEX (listed under both parent leafs) or a
  Nexus 7700 shared by several VDCs counts as one. Built-in parts (a chassis' own route
  processor or front-panel ports) are not counted, nor are devices left out by exclusion rules
  (their parts are counted in the separate excluded-devices workbook).

## Sanitized summary

**Outputs -> Sanitized summary** writes a report of counts only (file layouts, commands found,
part kinds, what could not be parsed), with no hostnames, IPs, serials or versions. Use it when
a parsing problem has to be described outside the network.

## Fixing parsers against real output

```
python tools/debug_parse.py input\<results file or folder> --device HOSTNAME --raw
python -m unittest discover -s tests     # back-tests every real-output fixture you saved
```

See [REFINING.md](REFINING.md). (The synthetic test suite, sample data and design notes are kept
by the developer and are not part of this repository.)
