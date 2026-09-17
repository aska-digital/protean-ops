#!/usr/bin/env python3
"""G-6 inflight gate — reads ops/INFLIGHT.md (live writer leases).

Rules enforced (control-plane procedure skill §B):
  1. No two ACTIVE claims on the same tree may write overlapping file paths
     (disjoint file sets on a shared tree are permitted under C3).
  2. An ACTIVE claim past its lease expiry is a crashed claim — it blocks
     overlapping writes until classified, so it is reported as a violation.
  3. Released rows, the empty-state row, and rows inside HTML comments
     (ILLUSTRATIVE examples) are ignored.
  4. An active row whose lease expiry is unparseable is a violation (lease unverifiable).

Usage:
  check-inflight.py [PATH-to-INFLIGHT.md]
  Default record: $PROTEAN_RECORDS_ROOT or ./records/INFLIGHT.md
Exit codes: 0 pass | 1 violation | 2 missing/unparseable required input. Read-only tool.
"""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

NCOL = 11  # claim id, session id, profile, provider/model, tree, owned files,
           # claim time, lease expiry, release time, conflict result, status


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


def tree_key(cell):
    return re.sub(r"\s+", " ", cell.replace("`", "").lower()).strip()


def file_tokens(cell):
    """Normalize an owned-files cell to comparable path tokens."""
    toks = set()
    for part in re.split(r"[,;]", cell):
        t = part.replace("`", "").split("(")[0].strip().strip(".")
        if "/" in t:
            toks.add(t.rstrip("/").lower())
    return toks


def parse_iso(cell):
    s = re.sub(r"\s*\(.*\)\s*$", "", cell).strip().strip("`")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv):
    args = [a for a in argv]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    record = args[0] if args else None
    path = Path(record) if record else default_record("INFLIGHT.md")
    if not path.is_file():
        print("FAIL: inflight record not found: %s" % path)
        return 2
    rows = parse_section_table(path.read_text(errors="replace"), "## Lease rows")
    if not rows:
        print("FAIL: no '## Lease rows' table parsed in %s" % path)
        return 2
    data, active = [], []
    for cells in rows:
        cid = cells[0]
        if "empty state" in cid.lower() or cid in ("claim id", "---"):
            continue
        data.append(cells)
        if len(cells) != NCOL:
            print("FAIL: %s claim '%s': %d cells, expected %d" % (path.name, cid, len(cells), NCOL))
            continue
        if cells[10].strip().lower() == "active":
            active.append(cells)
    v = []
    now = datetime.now(timezone.utc)
    for cells in active:
        cid, expiry = cells[0], cells[7]
        dt = parse_iso(expiry)
        if dt is None:
            v.append("FAIL: %s claim '%s': lease expiry unparseable: '%s'" % (path.name, cid, expiry))
        elif dt <= now:
            v.append("FAIL: %s claim '%s': ACTIVE claim past lease expiry '%s' — crashed claim; "
                     "classify before any overlapping write" % (path.name, cid, expiry))
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            if tree_key(a[4]) == tree_key(b[4]):
                overlap = file_tokens(a[5]) & file_tokens(b[5])
                if overlap:
                    v.append("FAIL: %s claims '%s' + '%s': overlapping active writers on one tree, "
                             "shared paths: %s" % (path.name, a[0], b[0], ", ".join(sorted(overlap))))
    return report("check-inflight", v, scanned=len(data), src=str(path))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
