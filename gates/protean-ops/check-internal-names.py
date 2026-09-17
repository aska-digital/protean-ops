#!/usr/bin/env python3
"""check-internal-names.py - leak gate for private identifiers.

Two independent detectors:

  D1 token digests  - every alphanumeric token in every scanned file is
                      lower-cased and hashed; a digest that appears in
                      gates/internal-names.blocklist is a leak. The blocklist
                      ships digests only, so this file never carries a private
                      name.
  D2 path shapes    - regexes that match machine-specific absolute paths and
                      private knowledge-base markers without spelling a single
                      example of one.

Usage: python3 gates/check-internal-names.py [path/to/repo]
Exit: 0 clean; 1 leak found.
"""
import hashlib
import os
import re
import sys

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Written so that no literal instance of a blocked path shape appears here.
PATH_RES = [
    re.compile(r"/" + r"(?:Users|home|Volumes)" + r"/"),
    re.compile(r"~/\.(?:hermes|config|local|cache|ssh)"),
    re.compile(r"(?:^|[^A-Za-z0-9_-])" + r"team[-_]skills" + r"(?:[^A-Za-z0-9_-]|$)"),
    re.compile(r"cache" + r"/" + r"delegation"),
    re.compile(r"(?:^|[^A-Za-z0-9_])" + r"profiles" + r"/"),
    re.compile(r"Documents" + r"/" + r"ai work"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
SKIP_EXT = (".pyc", ".so", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
            ".woff", ".woff2", ".ttf", ".ico")
# The blocklist sits beside this script, so the gate runs wherever it is
# installed, with no dependency on the repository layout.
HERE = os.path.dirname(os.path.abspath(__file__))
SELF_EXCLUDE = {"check-internal-names.py", "internal-names.blocklist"}


def load_blocklist(repo=None):
    path = os.path.join(HERE, "internal-names.blocklist")
    digests = set()
    if not os.path.exists(path):
        raise SystemExit("gate error: internal-names.blocklist missing beside this gate")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                digests.add(line)
    return digests


def scan(repo, digests):
    findings = []
    scanned = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_EXT):
                continue
            abspath = os.path.join(root, name)
            rel = os.path.relpath(abspath, repo).replace(os.sep, "/")
            if name in SELF_EXCLUDE or not os.path.isfile(abspath):
                continue
            scanned += 1
            try:
                with open(abspath, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in TOKEN_RE.finditer(line):
                    digest = hashlib.sha256(
                        match.group(0).lower().encode("utf-8")).hexdigest()
                    if digest in digests:
                        findings.append((rel, lineno, "token-digest"))
                for rx in PATH_RES:
                    if rx.search(line):
                        findings.append((rel, lineno, "private-path"))
    return scanned, findings


def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    digests = load_blocklist(repo)
    scanned, findings = scan(repo, digests)
    if findings:
        print("LEAK: " + str(len(findings)) + " finding(s)")
        for rel, lineno, kind in findings[:40]:
            print("  " + rel + ":" + str(lineno) + " [" + kind + "]")
        print("files scanned: " + str(scanned))
        return 1
    print("clean: " + str(scanned) + " files scanned, 0 findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
