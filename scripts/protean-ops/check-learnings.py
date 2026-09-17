#!/usr/bin/env python3
"""G-7 learnings gate — reads ops/LEARNINGS.md (incident intake rows).

Rules enforced (control-plane procedure skill §F):
  1. Every data row carries all nine fields: learning id, incident, root cause,
     one bounded rule, canonical home, enforcement surface, independent verifier,
     status, evidence path.
  2. No field may be empty or a placeholder (`—`, `TBD`, `<template>`, `__TOKEN__`).
  3. Exactly one learning id per row, shaped `lrn-<n>`, and unique across rows.

Usage:
  check-learnings.py [PATH-to-LEARNINGS.md]
  Default record: $PROTEAN_RECORDS_ROOT or ./records/LEARNINGS.md
Exit codes: 0 pass | 1 violation | 2 missing/unparseable required input. Read-only tool.
"""
import os
import re
import sys
from pathlib import Path

FIELDS = ["learning id", "incident", "root cause", "one bounded rule",
          "canonical home", "enforcement surface", "independent verifier",
          "status", "evidence path"]
NCOL = len(FIELDS)
PLACEHOLDER = {"", "-", "—", "–", "n/a", "na", "tbd", "todo", "fixme", "?", "placeholder", "none"}


def default_record(name):
    """Records root: $PROTEAN_RECORDS_ROOT, else the repository's ./records."""
    root = os.environ.get("PROTEAN_RECORDS_ROOT")
    base = Path(root).expanduser() if root else Path(__file__).resolve().parent.parent.parent / "records"
    return base / name


def parse_section_table(text, heading_prefix):
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


def filled(cell):
    c = cell.strip().strip("`").strip()
    if c.lower() in PLACEHOLDER:
        return False
    if re.fullmatch(r"<.*>", c) or re.search(r"__[A-Z_]+__", c):
        return False
    return len(c) > 0


def check_rows(rows, src):
    v, seen = [], {}
    for cells in rows:
        rid = cells[0] if cells else "?"
        if rid.strip().lower() == "learning id":
            continue
        if len(cells) != NCOL:
            v.append("FAIL: %s row '%s': %d cells, expected %d" % (src, rid, len(cells), NCOL))
            continue
        lid = rid.strip()
        if not re.fullmatch(r"lrn-\d+", lid):
            v.append("FAIL: %s row '%s': learning id must be exactly one id shaped lrn-<n>" % (src, rid))
        elif lid in seen:
            v.append("FAIL: %s row '%s': duplicate learning id (one id per row, per row unique)" % (src, lid))
        else:
            seen[lid] = True
        for j, field in enumerate(FIELDS):
            if j == 0:
                continue  # id shape already enforced above
            if not filled(cells[j]):
                v.append("FAIL: %s row '%s': field '%s' empty or placeholder: '%s'"
                         % (src, lid, field, cells[j][:50]))
    return v


def main(argv):
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    record = args[0] if args else None
    path = Path(record) if record else default_record("LEARNINGS.md")
    if not path.is_file():
        print("FAIL: learnings record not found: %s" % path)
        return 2
    rows = parse_section_table(path.read_text(errors="replace"), "## Rows")
    if not rows:
        print("FAIL: no '## Rows' table parsed in %s" % path)
        return 2
    v = check_rows(rows, path.name)
    return report("check-learnings", v, scanned=len(rows), src=str(path))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
