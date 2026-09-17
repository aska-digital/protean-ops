#!/usr/bin/env python3
"""G-13 hot-path freeze gate — verifies current hot-path bytes against a recorded manifest.

The manifest is md5sum-shaped: `MD5  path` (two spaces or a tab), `#` comments. Paths
may be absolute, `~`-relative, or relative to `$PROTEAN_HOME` when that variable is
set. The manifest itself defines the hot-path set; this gate invents no canonical home.

Rules (control-plane procedure skill §B/§D):
  1. Every listed file must exist; its current md5 must equal the recorded value.
  2. A changed file is a violation UNLESS an explicit --allow-change RECEIPT is given
     (an existing, non-empty receipt documenting the gate-passed update). Allowed
     changes are still listed in the summary.
  3. This tool NEVER writes the manifest or any input.

Usage:
  check-hotpath-freeze.py MANIFEST.md5 [--allow-change RECEIPT.md]
Exit codes: 0 pass (or all changes covered by a receipt) | 1 changed without receipt
| 2 missing/unparseable required input. Read-only tool.
"""
import hashlib
import os
import sys
from pathlib import Path


def resolve(rec_path, manifest_dir):
    """Resolve a manifest path: absolute, else under $PROTEAN_HOME, else the manifest dir."""
    p = Path(rec_path).expanduser()
    if p.is_absolute():
        return p
    home = os.environ.get("PROTEAN_HOME")
    bases = ([Path(home).expanduser()] if home else []) + [Path(manifest_dir)]
    for base in bases:
        cand = base / p
        if cand.is_file():
            return cand
    return bases[-1] / p  # report under the last base when missing


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    allow = None
    if "--allow-change" in args:
        k = args.index("--allow-change")
        if k + 1 >= len(args):
            print("FAIL: --allow-change requires a receipt path argument")
            return 2
        allow = args[k + 1]
        del args[k:k + 2]
    if not args:
        print("FAIL: no manifest path supplied")
        return 2
    mp = Path(args[0]).expanduser()
    if not mp.is_file():
        print("FAIL: manifest not found: %s" % mp)
        return 2
    receipt = None
    if allow:
        receipt = Path(allow).expanduser()
        if not receipt.is_file() or not receipt.read_text(errors="replace").strip():
            print("FAIL: --allow-change receipt missing or empty: %s" % receipt)
            return 2
    entries = []
    for line in mp.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if len(parts) != 2 or len(parts[0]) not in (32, 64):
            print("FAIL: manifest line malformed (expected 'MD5  path'): %s" % s[:80])
            return 2
        if len(parts[0]) == 64:
            print("FAIL: manifest hash is not md5 (32 hex): %s" % s[:80])
            return 2
        entries.append((parts[0], parts[1].strip()))
    if not entries:
        print("FAIL: manifest lists no entries: %s" % mp)
        return 2
    v, changed, ok = [], [], 0
    for want, rec_path in entries:
        p = resolve(rec_path, mp.parent)
        if not p.is_file():
            v.append("FAIL: hot-path file missing: %s" % p)
            continue
        got = md5_of(p)
        if got == want:
            ok += 1
        else:
            changed.append("%s (recorded %s…, now %s…)" % (p, want[:8], got[:8]))
            if not allow:
                v.append("FAIL: hot-path changed without receipt: %s" % p)
    for c in changed:
        tag = "CHANGED (allowed by receipt %s)" % receipt if allow else "CHANGED"
        print("%s: %s" % (tag, c))
    if v:
        for line in v:
            print(line)
    print("check-hotpath-freeze: %s  files=%d  intact=%d  changed=%d  missing=%d%s"
          % ("FAIL" if v else "PASS", len(entries), ok, len(changed),
             len(entries) - ok - len(changed),
             "" if not allow else "  allow-change=%s" % receipt))
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
