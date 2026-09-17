#!/usr/bin/env python3
"""G-8 decision-report gate — checks an HTML decision report (control-plane procedure skill §C).

No canonical report home is invented: reports are supplied explicitly, either as
positional HTML paths or via a manifest file listing decision artifact paths.

Rules enforced per report:
  1. The file exists and is non-empty.
  2. Self-contained markup: contains `<style>`; no external asset fetch
     (stylesheet <link>, <script src>, @import/url() to http(s), remote <img>).
  3. The report is referenced (path or basename) from the supplied --evidence
     file or the supplied --manifest. A report given with no manifest and no
     evidence file is UNLINKED and fails.
Non-`.html` manifest lines are decision artifacts and must exist on disk.

Usage:
  check-decision-report.py REPORT.html [REPORT2.html ...]
      [--manifest PATH] [--evidence PATH]
Exit codes: 0 pass | 1 violation | 2 missing/unparseable required input. Read-only tool.
"""
import re
import sys
from pathlib import Path

EXT_ASSET_RES = [
    re.compile(r"<link\b[^>]*href=[\"']https?://", re.I),
    re.compile(r"<script\b[^>]*src=[\"']https?://", re.I),
    re.compile(r"@import\s+[\"']?https?://", re.I),
    re.compile(r"url\(\s*[\"']?https?://", re.I),
    re.compile(r"<img\b[^>]*src=[\"']https?://", re.I),
]


def manifest_artifacts(text):
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("-*").strip().strip("`").strip()
        if not s or s.startswith("#") or s.startswith("|"):
            continue
        m = re.search(r"([^\s`'\"<>]+\.html?)", s, re.I)
        if m:
            out.append(m.group(1))
        elif re.fullmatch(r"[\w~@./ -]+", s):
            out.append(s)
    return out


def check_report(rpt, texts):
    """texts: list of (label, content) where a reference to rpt satisfies linkage."""
    v = []
    p = Path(rpt).expanduser()
    if not p.is_file():
        return ["FAIL: decision report missing: %s" % p]
    if p.suffix.lower() not in (".html", ".htm"):
        return ["FAIL: decision report is not an HTML file: %s" % p]
    try:
        body = p.read_text(errors="replace")
    except OSError as e:
        return ["FAIL: decision report unreadable: %s (%s)" % (p, e)]
    if not body.strip():
        v.append("FAIL: decision report empty: %s" % p)
    if "<style" not in body.lower():
        v.append("FAIL: decision report not self-contained (no <style>): %s" % p)
    for rx in EXT_ASSET_RES:
        m = rx.search(body)
        if m:
            v.append("FAIL: decision report fetches an external asset (%s...): %s"
                     % (m.group(0)[:40], p))
            break
    linked = any(p.name in content or str(p) in content for _, content in texts)
    if not linked:
        v.append("FAIL: decision report not referenced by any supplied manifest/evidence: %s" % p)
    return v


def main(argv):
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    manifest = evidence = None
    for flag in ("--manifest", "--evidence"):
        if flag in args:
            k = args.index(flag)
            if k + 1 >= len(args):
                print("FAIL: %s requires a path argument" % flag)
                return 2
            val = args[k + 1]
            del args[k:k + 2]
            if flag == "--manifest":
                manifest = val
            else:
                evidence = val
    reports = [a for a in args if not a.startswith("-")]
    if not reports and not manifest:
        print("FAIL: no report path and no manifest supplied "
              "(see --help for the usage block)")
        return 2
    texts = []
    v = []
    if manifest:
        mp = Path(manifest).expanduser()
        if not mp.is_file():
            print("FAIL: manifest not found: %s" % mp)
            return 2
        mtext = mp.read_text(errors="replace")
        texts.append(("manifest", mtext))
        for art in manifest_artifacts(mtext):
            ap = Path(art).expanduser()
            if not ap.is_absolute():
                ap = (mp.parent / ap).resolve()
            if ap.suffix.lower() in (".html", ".htm"):
                if str(ap) not in reports:
                    reports.append(str(ap))
            elif not ap.is_file():
                v.append("FAIL: manifest artifact missing: %s" % ap)
    if evidence:
        ep = Path(evidence).expanduser()
        if not ep.is_file():
            print("FAIL: evidence file not found: %s" % ep)
            return 2
        texts.append(("evidence", ep.read_text(errors="replace")))
    if not reports:
        print("FAIL: manifest lists no .html decision report")
        return 2
    for rpt in reports:
        v += check_report(rpt, texts)
    if v:
        for line in v:
            print(line)
    print("check-decision-report: %s  scanned=%d  violations=%d"
          % ("FAIL" if v else "PASS", len(reports), len(v)))
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
