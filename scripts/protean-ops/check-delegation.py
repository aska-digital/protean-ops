#!/usr/bin/env python3
"""Delivery-receipt gate — proves a delivery was built by named specialists.

A delivery receipt is a markdown file carrying one DELEGATION-RECEIPT block.
The block lists the specialist lanes that produced the work, one per line:

  <!-- DELEGATION-RECEIPT begin -->
  specialist=build lane="harbor-build" stage="s4 build" evidence="cache/harbor-build-receipt.md"
  specialist=qa lane="harbor-qa" stage="s5 verify" evidence="cache/harbor-qa-verdict.md"
  <!-- DELEGATION-RECEIPT end -->

Rules enforced:
  1. The file exists and is non-empty.
  2. Exactly one receipt block: open/close markers each on their own line,
     open before close. A missing or unparseable block is a record problem.
  3. At least one specialist lane line inside the block. A block with zero
     lanes is a presumed self-made delivery and fails.
  4. Every lane names a specialist role, a lane name, a stage label, and an
     evidence pointer (specialist=, lane=, stage=, evidence=).
  5. Specialist roles come from the role manifest only (--roles,
     $PROTEAN_ROSTER, or ROSTER.txt beside the receipt); any other value is
     rejected. The gate fails closed when no manifest is available.
  6. No placeholder values: angle brackets in any field, or a field that is
     empty, `none`, or `test`, fails the lane.
  7. The receipt file must not be world-writable.

Usage:
  check-delegation.py RECEIPT.md [--roles PATH-to-ROSTER.txt]
Exit codes: 0 pass | 2 usage | 3 missing/unparseable record | 5 integrity
violation. Read-only tool. Stdlib only.
"""
import os
import re
import sys
from pathlib import Path

EXIT_PASS, EXIT_USAGE, EXIT_RECORD, EXIT_INTEGRITY = 0, 2, 3, 5

OPEN_MARKER = "<!-- DELEGATION-RECEIPT begin -->"
CLOSE_MARKER = "<!-- DELEGATION-RECEIPT end -->"

REQUIRED_KEYS = ("specialist", "lane", "stage", "evidence")
KV_RE = re.compile(r"([A-Za-z_]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|(\S+))")
PLACEHOLDER_VALUES = {"", "none", "test", "tests", "tbd", "todo", "placeholder",
                      "fixme", "xxx", "n/a", "na", "?"}

DEFAULT_ROSTER_FILE = "ROSTER.txt"


def load_roles(record_path, explicit=None):
    """Role manifest: --roles, $PROTEAN_ROSTER, or ROSTER.txt beside the record."""
    raw = None
    if explicit:
        try:
            raw = Path(explicit).expanduser().read_text(errors="replace")
        except OSError:
            return None
    elif os.environ.get("PROTEAN_ROSTER"):
        raw = os.environ["PROTEAN_ROSTER"]
    else:
        candidate = Path(record_path).resolve().parent / DEFAULT_ROSTER_FILE
        if candidate.is_file():
            try:
                raw = candidate.read_text(errors="replace")
            except OSError:
                return None
        else:
            return None
    roles = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("roles:"):
            s = s[len("roles:"):]
        for tok in re.split(r"[,\s]+", s):
            tok = tok.strip().lower()
            if tok:
                roles.add(tok)
    return roles


def parse_block(text):
    """Return (block_lines, error). error is None on success."""
    lines = text.splitlines()
    opens = [i for i, l in enumerate(lines) if l.strip() == OPEN_MARKER]
    closes = [i for i, l in enumerate(lines) if l.strip() == CLOSE_MARKER]
    if not opens and not closes:
        return None, "no DELEGATION-RECEIPT block (missing open/close markers)"
    if len(opens) != 1 or len(closes) != 1:
        return None, ("DELEGATION-RECEIPT block markers must appear exactly once "
                      "(open=%d close=%d)" % (len(opens), len(closes)))
    if closes[0] < opens[0]:
        return None, "DELEGATION-RECEIPT close marker precedes the open marker"
    return lines[opens[0] + 1:closes[0]], None


def parse_lane(line):
    """Return (fields, error)."""
    fields = {}
    for m in KV_RE.finditer(line):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5))
        fields.setdefault(key, val)
    missing = [k for k in REQUIRED_KEYS if k not in fields]
    if missing:
        return None, "lane line missing %s: %s" % ("/".join(missing), line.strip()[:80])
    return fields, None


def check_placeholder(key, val):
    if "<" in val or ">" in val:
        return "lane field %s carries angle-bracket placeholder: %r" % (key, val)
    if val.strip().lower() in PLACEHOLDER_VALUES:
        return "lane field %s is a placeholder (%r)" % (key, val)
    return None


def check_receipt(path, roles):
    violations = []
    p = Path(path).expanduser()
    if not p.is_file():
        return None, ["receipt not found: %s" % p]
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        return None, ["receipt unreadable: %s (%s)" % (p, e)]
    if not text.strip():
        return None, ["receipt empty: %s" % p]
    block, err = parse_block(text)
    if err is not None:
        return None, [err + ": %s" % p]
    lanes = [l for l in block if "specialist" in l and "=" in l]
    if not lanes:
        violations.append("no specialist lanes declared (presumed self-made): %s" % p)
        return violations, None
    if roles is None:
        return None, ["no role manifest available (--roles, $PROTEAN_ROSTER, "
                      "or ROSTER.txt beside the receipt)"]
    for line in lanes:
        fields, err = parse_lane(line)
        if err is not None:
            violations.append(err)
            continue
        assert fields is not None
        role = fields["specialist"].strip().lower()
        if role not in roles:
            violations.append("specialist role '%s' not on the manifest" % fields["specialist"])
        for key in REQUIRED_KEYS:
            perr = check_placeholder(key, fields[key].strip())
            if perr is not None:
                violations.append(perr + " in lane: %s" % line.strip()[:80])
    try:
        if p.stat().st_mode & 0o002:
            violations.append("receipt is world-writable: %s" % p)
    except OSError:
        pass
    return violations, None


def main(argv):
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return EXIT_PASS
    roles_path = None
    if "--roles" in args:
        k = args.index("--roles")
        if k + 1 >= len(args):
            print("FAIL: --roles requires a path argument")
            return EXIT_USAGE
        roles_path = args[k + 1]
        del args[k:k + 2]
    rest = [a for a in args if not a.startswith("-")]
    if len(rest) != 1 or any(a.startswith("-") for a in args):
        print("FAIL: usage: check-delegation.py RECEIPT.md [--roles PATH] (see --help)")
        return EXIT_USAGE
    roles = load_roles(rest[0], roles_path)
    violations, record_err = check_receipt(rest[0], roles)
    if record_err is not None:
        for e in record_err:
            print("FAIL: %s" % e)
        return EXIT_RECORD
    if violations:
        for v in violations:
            print("FAIL: %s" % v)
        return EXIT_INTEGRITY
    print("check-delegation: PASS")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
