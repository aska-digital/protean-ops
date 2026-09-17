#!/usr/bin/env python3
"""G-5 rotation gate — reads ops/ROTATION-STATE.md (optional: current-era DISPATCH-LEDGER rows).

Rules enforced (control-plane procedure skill §B):
  1. No two consecutive rows assign the same next worker unless the later row carries an
     explicit, non-empty exception reason (bare `none` / dashes are not exceptions).
  2. Worker cells carry role names from the roster only (--roster, $PROTEAN_ROSTER, or
     <record dir>/ROSTER.txt); any other value is rejected.
  3. The `verified provider/model` receipt cell is non-empty and provider/model-shaped.
  4. (--ledger PATH) current-era ledger rows carry a non-empty provider/model receipt.
     The consecutive rule is evaluated on rotation state only.

Usage:
  check-rotation.py [PATH-to-ROTATION-STATE.md] [--ledger PATH-to-DISPATCH-LEDGER.md]
  Default record: $PROTEAN_RECORDS_ROOT or ./records/ROTATION-STATE.md
Exit codes: 0 pass | 1 violation | 2 missing/unparseable required input. Read-only tool.
"""
import os
import re
import sys
from pathlib import Path

COLS = ["handoff id", "boundary time", "previous worker", "next worker",
        "stage / unit", "capability justification", "verified provider/model",
        "exception reason", "release state"]
NCOL = len(COLS)
DEFAULT_ROSTER_FILE = "ROSTER.txt"
PLACEHOLDER = {"", "-", "—", "–", "none", "n/a", "na", "tbd", "todo", "?"}


def load_roster(record_path, explicit=None):
    """Roster allowlist: --roster, $PROTEAN_ROSTER, or ROSTER.txt beside the record."""
    if explicit:
        raw = Path(explicit).expanduser().read_text(errors="replace") if Path(
            explicit).expanduser().is_file() else explicit
    elif os.environ.get("PROTEAN_ROSTER"):
        raw = os.environ["PROTEAN_ROSTER"]
    else:
        candidate = Path(record_path).resolve().parent / DEFAULT_ROSTER_FILE
        if not candidate.is_file():
            return None
        raw = candidate.read_text(errors="replace")
    names = set()
    for chunk in re.split(r"[,\n]", raw):
        token = chunk.split(":")[-1].strip().strip("`").lower()
        if token and not token.startswith("#"):
            names.add(token)
    return names


def default_record(name):
    """Records root: $PROTEAN_RECORDS_ROOT, else the repository's ./records."""
    root = os.environ.get("PROTEAN_RECORDS_ROOT")
    base = Path(root).expanduser() if root else Path(__file__).resolve().parent.parent.parent / "records"
    return base / name


def cell_ok(cell):
    c = cell.strip().strip("`").strip()
    if c.lower() in PLACEHOLDER:
        return False
    if re.fullmatch(r"<.*>", c) or re.search(r"__[A-Z_]+__", c):
        return False
    return True


def parse_section_table(text, heading_prefix):
    """Data rows of the first markdown table under the `heading_prefix` section.

    Returns list of cell-lists (header and --- separator excluded). Lines inside
    HTML comments and illustrative/definition rows are skipped by the caller."""
    lines = text.splitlines()
    out, started, in_comment, header_seen = [], False, False, False
    for line in lines:
        s = line.strip()
        if s.startswith("<!--"):
            in_comment = True
        if in_comment:
            if "-->" in s:
                in_comment = False
            continue
        if s.startswith("## "):
            if started:
                break
            started = s.startswith(heading_prefix)
            continue
        if not started or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        if not header_seen:
            header_seen = True
            continue
        out.append(cells)
    return out


def report(gate, violations, scanned, src):
    if violations:
        for v in violations:
            print(v)
    print("%s: %s  scanned=%d  violations=%d  src=%s"
          % (gate, "FAIL" if violations else "PASS", scanned, len(violations), src))
    return 1 if violations else 0


def check_rotation_rows(rows, src, roster=None):
    v = []
    for i, cells in enumerate(rows):
        rid = cells[0] if cells else "row %d" % (i + 1)
        if len(cells) != NCOL:
            v.append("FAIL: %s row '%s': %d cells, expected %d" % (src, rid, len(cells), NCOL))
            continue
        for j, name in ((2, "previous worker"), (3, "next worker")):
            w = cells[j].strip().lower()
            if roster is None or w not in roster:
                v.append("FAIL: %s row '%s': %s '%s' not on the roster" % (src, rid, name, cells[j]))
        pm = cells[6]
        if not cell_ok(pm) or "/" not in pm:
            v.append("FAIL: %s row '%s': verified provider/model receipt empty or not a provider/model pair: '%s'"
                     % (src, rid, pm[:60]))
    for i in range(1, len(rows)):
        if len(rows[i]) != NCOL or len(rows[i - 1]) != NCOL:
            continue
        prev_next = rows[i - 1][3].strip().lower()
        cur_next = rows[i][3].strip().lower()
        if prev_next and prev_next == cur_next:
            exc = rows[i][7].strip()
            if not cell_ok(exc) or exc.lower() in PLACEHOLDER:
                v.append("FAIL: %s rows '%s' → '%s': consecutive assignment to '%s' without an "
                         "explicit non-empty exception reason"
                         % (src, rows[i - 1][0], rows[i][0], rows[i][3]))
    return v


def check_ledger_rows(path):
    """Current-era ledger section: fixed 15-col rows; legacy names + model receipt only."""
    p = Path(path)
    if not p.is_file():
        return ["FAIL: ledger not found: %s" % p], []
    rows = parse_section_table(p.read_text(errors="replace"),
                               "## Current-era dispatch schema")
    data = [r for r in rows if len(r) == 15 and r[0].strip() not in ("task id", "---")]
    v = []
    for cells in data:
        tid = cells[0]
        for j, name in ((1, "owner profile"), (2, "previous worker")):
            if len(cells[j].strip()) == 0:
                v.append("FAIL: ledger row '%s': %s is empty" % (tid, name))
        pm = cells[3]
        if not cell_ok(pm) or "/" not in pm:
            v.append("FAIL: ledger row '%s': provider/model receipt empty or not a pair: '%s'"
                     % (tid, pm[:60]))
    return v, [r[0] for r in data]


def main(argv):
    ledger = None
    record = None
    roster_arg = None
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--ledger" in args:
        k = args.index("--ledger")
        if k + 1 >= len(args):
            print("FAIL: --ledger requires a path argument")
            return 2
        ledger = args[k + 1]
        del args[k:k + 2]
    if "--roster" in args:
        k = args.index("--roster")
        if k + 1 >= len(args):
            print("FAIL: --roster requires a path or comma-separated list")
            return 2
        roster_arg = args[k + 1]
        del args[k:k + 2]
    if args:
        record = args[0]
    path = Path(record) if record else default_record("ROTATION-STATE.md")
    if not path.is_file():
        print("FAIL: rotation record not found: %s" % path)
        return 2
    rows = parse_section_table(path.read_text(errors="replace"), "## Rows")
    if not rows:
        print("FAIL: no '## Rows' table parsed in %s" % path)
        return 2
    roster = load_roster(path, roster_arg)
    if roster is None:
        print("FAIL: roster unavailable: supply --roster, $PROTEAN_ROSTER, or "
              "ROSTER.txt beside the record")
        return 2
    v = check_rotation_rows(rows, path.name, roster)
    n = len(rows)
    if ledger:
        lv, ln = check_ledger_rows(ledger)
        v += lv
        n += len(ln)
    return report("check-rotation", v, scanned=n, src=str(path))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
