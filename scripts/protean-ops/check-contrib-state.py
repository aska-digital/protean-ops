#!/usr/bin/env python3
"""check-contrib-state.py - G-14, the contribution-state gate.

Reads the append-only contribution-state record and answers two questions:

  1. Is the recorded state legal? The toggle enum, the always-on internal
     assertion, and every row's shape.
  2. May the queried write happen now? Lane cap, rate windows, quiet hours,
     grant validity, per-repository burst detector.

The RULE this gate enforces lives in the control-plane skill; this gate owns the
VALUE. One flag, one home, fail closed:

  * an ABSENT record reads as `external_contrib: off` and `internal_contrib: on`
    (the documented fail-closed default): a state query passes, a remote-write
    query is refused;
  * an unreadable, unparseable, or illegal record is a violation, never a silent
    `off`;
  * a missing quiet-hours timezone database refuses an external write (exit 1)
    rather than passing it.

Modes:

  state check   --state <record>                      exit 0 legal, 1 illegal
  write check   --state <record> --target external
                --repo <owner/name> --action <class>  exit 0 allowed, 1 refused

Usage:
  check-contrib-state.py [--state PATH] [--now ISO8601] [--repo OWNER/NAME]
                         [--action pr|issue|comment|review|merge]
                         [--target internal|external] [--json] [--self-test]
                         [--help]

State resolution, in order: --state, $PROTEAN_CONTRIB_STATE, the invoking tree's
`ops/CONTRIB-STATE.md`, ./records/CONTRIB-STATE.md, ./ops/CONTRIB-STATE.md.

Exit: 0 pass | 1 violation or refused write | 2 missing or unusable input.
Read-only tool. Stdlib only.
"""
import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import sys
import tempfile

EXIT_PASS, EXIT_VIOLATION, EXIT_INPUT = 0, 1, 2

KINDS = ("epoch", "switch", "lane", "submitted", "grant", "consumed", "blocked", "note")
ACTIONS = ("pr", "issue", "comment", "review", "merge")
NCOL = 11

# Locked defaults (control-plane law; values are the bounded lane/rate/quiet-hour
# contract, tunable only by an amendment to the rule, never by a lane editing its
# own limits).
LANE_CAP_TOTAL = 2
LANE_CAP_EXTERNAL = 1
QUIET_START_HOUR = 5
QUIET_END_HOUR = 21
QUIET_TZ = "America/New_York"
RATES = {
    "pr": [(24, 1, "any"), (24 * 7, 3, "any")],
    "issue": [(24, 1, "any"), (24 * 7, 1, "repo")],
    "comment": [(24, 2, "any"), (24 * 7, 5, "any")],
    "review": [(24, 2, "any"), (24 * 7, 5, "any")],
}
BURST_WRITE_CAP = 3
BURST_WINDOW_H = 1
THREAD_WRITE_CAP = 1
THREAD_WINDOW_H = 24
GRANT_MAX_DAYS = 14
GRANT_MAX_ACTIONS = 3


class Unusable(Exception):
    """Input that cannot be checked at all: exit 2."""


def rule():
    return "-" * 72


def parse_iso(text):
    raw = (text or "").strip().strip("`")
    raw = re.sub(r"\s*\(.*\)\s*$", "", raw).strip()
    if not raw or raw in ("-", "—", "n/a", "pending"):
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def cells_of(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(cells):
    return bool(cells) and all(c and set(c) <= set("-: ") for c in cells)


def section_rows(text, heading):
    """Cells of every table row under `heading`, ignoring fenced HTML comments."""
    out, inside, in_comment, header_seen = [], False, False, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("<!--"):
            in_comment = True
        if in_comment:
            if "-->" in s:
                in_comment = False
            continue
        if s.startswith("## "):
            if inside and not s.startswith(heading):
                break
            inside = s.startswith(heading)
            continue
        if not inside or not s.startswith("|"):
            continue
        cells = cells_of(s)
        if is_separator(cells):
            continue
        if not header_seen:
            header_seen = True
            continue
        out.append(cells)
    return out


def parse_header(text):
    header = {}
    for cells in section_rows(text, "## Header"):
        if len(cells) >= 2:
            header[cells[0].strip().lower()] = cells[1].strip()
    return header


def parse_rows(text):
    rows, shape = [], []
    for cells in section_rows(text, "## Rows"):
        if "empty state" in cells[0].lower():
            continue
        if len(cells) != NCOL:
            shape.append("row '%s' has %d cells, expected %d" % (cells[0], len(cells), NCOL))
            continue
        rows.append({
            "id": cells[0], "kind": cells[1].strip().lower(), "repo": cells[2],
            "action": cells[3].strip().lower(), "thread": cells[4],
            "mode": cells[5].strip().lower(), "authority": cells[6],
            "detail": cells[7], "lane": cells[8], "time": cells[9], "evidence": cells[10],
        })
    return rows, shape


def detail_fields(detail):
    return dict(re.findall(r"([A-Za-z_]+)=([^\s,;]+)", detail or ""))


def resolve_state(explicit):
    if explicit:
        return explicit
    env = os.environ.get("PROTEAN_CONTRIB_STATE")
    if env:
        return env
    script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(script_root, "ops", "CONTRIB-STATE.md"),
                  os.path.join(os.getcwd(), "records", "CONTRIB-STATE.md"),
                  os.path.join(os.getcwd(), "ops", "CONTRIB-STATE.md")]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


class State(object):
    def __init__(self, path):
        self.path = path
        self.absent = not os.path.isfile(path)
        self.digest = "-"
        self.internal = "on"
        self.external = "off"
        self.header = {}
        self.rows = []
        self.violations = []
        if self.absent:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise Unusable("state record is unreadable: %s (%s)" % (path, exc))
        self.digest = hashlib.md5(raw).hexdigest()
        text = raw.decode("utf-8", "replace")
        self.header = parse_header(text)
        self.rows, shape = parse_rows(text)
        self.violations.extend("row shape: " + s for s in shape)
        for key in ("schema_version", "internal_contrib", "external_contrib"):
            if key not in self.header:
                self.violations.append("header is missing the required key '%s'" % key)
        if "schema_version" in self.header and self.header["schema_version"] != "1":
            self.violations.append("schema_version must be 1, got '%s'"
                                   % self.header["schema_version"])
        self.internal = self.header.get("internal_contrib", "").strip().lower()
        self.external = self.header.get("external_contrib", "").strip().lower()
        if self.external not in ("on", "off"):
            self.violations.append("external_contrib must be 'on' or 'off', got '%s'"
                                   % self.header.get("external_contrib", ""))
            self.external = "off"
        # D18: internal is always on. 'off' is legal only with a cited decision.
        if self.internal != "on":
            cited = [r for r in self.rows if re.search(r"decision:\s*\S+", r["detail"] or "")]
            if not cited:
                self.violations.append(
                    "internal_contrib is '%s' with no 'decision: <id>' cited in any row"
                    % self.header.get("internal_contrib", ""))
        # A 'switch' to on is a recorded operator decision, never a bare field edit.
        if self.external == "on":
            switches = [r for r in self.rows
                        if r["kind"] == "switch"
                        and re.search(r"external_contrib\s*=\s*on", r["detail"] or "", re.I)]
            cited = [r for r in switches
                     if re.search(r"(operator|reference|ref)\s*[:=]?\s*\S+",
                                  (r["authority"] or "") + " " + (r["detail"] or ""), re.I)]
            if not cited:
                self.violations.append(
                    "external_contrib is 'on' with no 'switch' row citing an operator "
                    "reference: turning it on is a recorded decision, not a field edit")
        for row in self.rows:
            if row["kind"] not in KINDS:
                self.violations.append("row '%s': unknown kind '%s'" % (row["id"], row["kind"]))
            if row["mode"] and row["mode"] not in ("internal", "external"):
                self.violations.append("row '%s': mode must be internal or external, got '%s'"
                                       % (row["id"], row["mode"]))
            if row["action"] and row["action"] not in ACTIONS + ("-", "—"):
                self.violations.append("row '%s': unknown action '%s'"
                                       % (row["id"], row["action"]))
            if not parse_iso(row["time"]):
                self.violations.append("row '%s': time is not ISO-8601: '%s'"
                                       % (row["id"], row["time"]))

    def submitted(self, mode="external"):
        return [r for r in self.rows if r["kind"] == "submitted" and r["mode"] == mode]

    def live_lanes(self, now):
        live = []
        for row in self.rows:
            if row["kind"] != "lane":
                continue
            lease = parse_iso(detail_fields(row["detail"]).get("lease_until", ""))
            if lease is None or lease > now:
                live.append(row)
        return live

    def grants(self, now):
        out = {}
        for row in self.rows:
            if row["kind"] != "grant":
                continue
            gid = detail_fields(row["detail"]).get("grant_id", row["id"])
            fields = detail_fields(row["detail"])
            consumed = len([r for r in self.rows
                            if r["kind"] == "consumed" and gid in (r["authority"] or "")
                            and gid in (r["detail"] or "")])
            out[gid] = {"id": gid, "fields": fields, "consumed": consumed, "row": row}
        return out


def check_shape(state, now):
    """State-level rules that hold regardless of any queried write."""
    v = list(state.violations)
    live = state.live_lanes(now)
    if len(live) > LANE_CAP_TOTAL:
        v.append("live contribution lanes = %d, cap is %d" % (len(live), LANE_CAP_TOTAL))
    ext = [r for r in live if r["mode"] == "external"]
    if len(ext) > LANE_CAP_EXTERNAL:
        v.append("live external contribution lanes = %d, cap is %d"
                 % (len(ext), LANE_CAP_EXTERNAL))
    for gid, grant in sorted(state.grants(now).items()):
        fields = grant["fields"]
        for key in ("class", "scope", "expires", "max_actions"):
            if key not in fields:
                v.append("grant '%s': missing requirement '%s'" % (gid, key))
        expires = parse_iso(fields.get("expires", ""))
        if "expires" in fields and expires is None:
            v.append("grant '%s': expires is not ISO-8601: '%s'" % (gid, fields["expires"]))
        elif expires is not None and expires < now:
            v.append("grant '%s': expired at %s" % (gid, fields["expires"]))
        max_actions = fields.get("max_actions")
        if max_actions is not None and not re.match(r"^[0-9]+$", max_actions):
            v.append("grant '%s': max_actions must be an integer, got '%s'" % (gid, max_actions))
        elif max_actions is not None and int(max_actions) > GRANT_MAX_ACTIONS:
            v.append("grant '%s': max_actions %s exceeds the cap of %d"
                     % (gid, max_actions, GRANT_MAX_ACTIONS))
        elif max_actions is not None and grant["consumed"] > int(max_actions):
            v.append("grant '%s': %d consumption rows exceed max_actions=%s"
                     % (gid, grant["consumed"], max_actions))
    return v


def ny_now(now):
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None, "zoneinfo is unavailable in this interpreter"
    try:
        return now.astimezone(ZoneInfo(QUIET_TZ)), None
    except Exception as exc:  # ZoneInfoNotFoundError and friends
        return None, "timezone database for %s is unavailable (%s)" % (QUIET_TZ, exc)


def check_write(state, now, target, repo, action, thread=None):
    v = []
    if target == "internal":
        if state.internal != "on":
            v.append("internal_contrib is not 'on'")
        return v
    if action == "merge":
        v.append("an external merge is never autonomous (owner approval only)")
        return v
    if state.external != "on":
        v.append("external_contrib is 'off': no remote write of any kind is permitted "
                 "(a local branch, commit, test run, and rendered draft are allowed)")
        return v
    local, why = ny_now(now)
    if local is None:
        v.append("quiet hours cannot be verified, and an external write fails closed: %s" % why)
        return v
    if not (QUIET_START_HOUR <= local.hour < QUIET_END_HOUR):
        v.append("quiet hours: %s local time is outside %02d:00-%02d:00"
                 % (local.strftime("%H:%M"), QUIET_START_HOUR, QUIET_END_HOUR))
    for hours, limit, scope in RATES.get(action, []):
        rows = [r for r in state.submitted("external") if r["action"] == action]
        window = now - datetime.timedelta(hours=hours)
        in_window = [r for r in rows if (parse_iso(r["time"]) or window) >= window
                     and (scope == "any" or r["repo"] == repo)]
        if len(in_window) >= limit:
            v.append("rate: %d external %s row(s) in the last %dh (limit %d, scope %s)"
                     % (len(in_window), action, hours, limit, scope))
    if repo:
        burst = [r for r in state.submitted("external") if r["repo"] == repo
                 and (parse_iso(r["time"]) or now) >= now - datetime.timedelta(hours=BURST_WINDOW_H)]
        if len(burst) >= BURST_WRITE_CAP - 1:
            v.append("burst detector: %d write(s) to %s inside %dh; the next write is the "
                     "%dth and the switch flips to off pending review"
                     % (len(burst), repo, BURST_WINDOW_H, BURST_WRITE_CAP))
    if action in ("comment", "review") and thread:
        per_thread = [r for r in state.submitted("external")
                      if r["thread"] == thread
                      and (parse_iso(r["time"]) or now) >= now - datetime.timedelta(
                          hours=THREAD_WINDOW_H)]
        if len(per_thread) >= THREAD_WRITE_CAP:
            v.append("rate: %d write(s) to thread %s inside %dh (limit %d)"
                     % (len(per_thread), thread, THREAD_WINDOW_H, THREAD_WRITE_CAP))
    return v


def build_parser():
    p = argparse.ArgumentParser(add_help=False,
                                description="G-14 contribution-state gate "
                                            "(toggle legality and write limits)")
    p.add_argument("-h", "--help", action="store_true", dest="help",
                   help="print this help and exit")
    p.add_argument("record", nargs="?", help="state record path (positional form, "
                                             "matching the sibling gates)")
    p.add_argument("--state")
    p.add_argument("--now")
    p.add_argument("--repo")
    p.add_argument("--action", choices=list(ACTIONS))
    p.add_argument("--target", choices=["internal", "external"])
    p.add_argument("--thread")
    p.add_argument("--json", action="store_true")
    p.add_argument("--self-test", action="store_true")
    return p


def report(state, violations, now, target, repo, action, as_json):
    result = {
        "gate": "G-14",
        "state": state.path,
        "state_present": not state.absent,
        "state_md5": state.digest,
        "internal_contrib": state.internal,
        "external_contrib": state.external if not state.absent else "off",
        "fail_closed_default": state.absent,
        "read_at": now.isoformat().replace("+00:00", "Z"),
        "target": target,
        "repo": repo,
        "action": action,
        "read_line": "external_toggle = read_at=%s value=%s file_md5=%s ; internal_mode = %s"
                     % (now.isoformat().replace("+00:00", "Z"),
                        state.external if not state.absent else "off", state.digest,
                        "on (always-on)" if state.internal == "on" else state.internal),
        "verdict": "FAIL" if violations else "PASS",
        "violations": violations,
    }
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for line in violations:
            print("FAIL: " + line)
        print(result["read_line"])
        print("check-contrib-state: %s  scanned=%d  violations=%d  src=%s"
              % (result["verdict"], len(state.rows), len(violations), state.path))
    return EXIT_VIOLATION if violations else EXIT_PASS


def self_test():
    """Focused fixtures: one green state, one red state per locked rule."""
    cases = []

    def run(state_text, args, expect, label, now="2026-09-17T15:00:00Z"):
        tmp = tempfile.mkdtemp(prefix="contrib-state-")
        path = os.path.join(tmp, "CONTRIB-STATE.md")
        if state_text is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(state_text)
        argv = ["--state", path, "--now", now] + args
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(argv)
        cases.append((label, code, expect, code == expect, buf.getvalue()))

    green = (HEADER_TMPL % ("on", "off") + ROW_HEADER + "\n")
    run(green, ["--json"], EXIT_PASS, "green state, no write queried")
    run(green, [], EXIT_PASS, "green state, default query")
    run(None, [], EXIT_PASS, "absent record reads external=off, internal=on")
    run(None, ["--target", "external", "--repo", "o/r", "--action", "pr"], EXIT_VIOLATION,
        "absent record refuses an external write")
    run(HEADER_TMPL % ("on", "maybe"), [], EXIT_VIOLATION, "illegal toggle enum")
    run(HEADER_TMPL % ("off", "off"), [], EXIT_VIOLATION, "internal off without a decision id")
    run(HEADER_TMPL % ("off", "off") + ROW_HEADER
        + "| cc-1 | note | — | — | — | internal | — | decision: D-1 operator decision | lane | "
          "2026-09-17T10:00:00Z | evidence/x |\n", [], EXIT_PASS, "internal off with a cited decision id")
    run(green, ["--target", "internal", "--action", "pr", "--repo", "o/r"], EXIT_PASS,
        "internal write is always allowed")
    run(ON_STATE, ["--target", "external", "--repo", "o/r", "--action", "pr"], EXIT_PASS,
        "external write inside quiet hours with the toggle on")
    run(HEADER_TMPL % ("on", "on") + ROW_HEADER, [], EXIT_VIOLATION,
        "external on without a citing switch row")
    run(ON_STATE, ["--target", "external", "--repo", "o/r", "--action", "pr"],
        EXIT_VIOLATION, "external write outside quiet hours", now="2026-09-18T02:30:00Z")
    run(HEADER_TMPL % ("on", "off") + ROW_HEADER, ["--target", "external", "--repo", "o/r",
                                                   "--action", "pr"], EXIT_VIOLATION,
        "external write while the toggle is off")
    run(ON_STATE + "| cc-1 | submitted | o/r | pr | 12 | external | approval op-1 | — | lane | "
                   "2026-09-17T10:00:00Z | evidence/x |\n",
        ["--target", "external", "--repo", "o/r", "--action", "pr"], EXIT_VIOLATION,
        "rate: a second external PR inside 24h")
    lanes = ON_STATE + "".join(
        "| cc-l%d | lane | o/r | pr | — | external | internal-default | lease_until=2026-09-17T20:00:00Z | "
        "lane | 2026-09-17T10:00:00Z | evidence/x |\n" % i for i in (1, 2, 3))
    run(lanes, [], EXIT_VIOLATION, "lane cap: three live lanes")
    burst = ON_STATE + "".join(
        "| cc-b%d | submitted | o/r | comment | %d | external | internal-default | — | lane | "
        "2026-09-17T14:%02d:00Z | evidence/x |\n" % (i, i, 40 + i) for i in (1, 2))
    run(burst, ["--target", "external", "--repo", "o/r", "--action", "comment"], EXIT_VIOLATION,
        "burst detector: a third write to one repo inside an hour")
    run(green + "| cc-g | grant | o/r | pr | — | external | grant g1 | grant_id=g1 class=pr-open "
        "scope=o/r expires=2026-09-01T00:00:00Z max_actions=2 | lane | 2026-09-01T00:00:00Z | "
        "evidence/x |\n", [], EXIT_VIOLATION, "grant: expired")
    run(green + "| cc-g | grant | — | — | — | external | grant g2 | grant_id=g2 class=pr-open "
        "scope=o/r max_actions=9 | lane | 2026-09-01T00:00:00Z | evidence/x |\n", [],
        EXIT_VIOLATION, "grant: missing requirements and an over-cap action count")
    run(green + "| cc-x | submitted | o/r | pr | 1 | external | internal-default | — | lane | "
        "not-a-date | evidence/x |\n", [], EXIT_VIOLATION, "row with an unparseable timestamp")
    run("# CONTRIB-STATE\n\n## Header\n\n| key | value |\n|---|---|\n| external_contrib | off |\n"
        "\n## Rows\n\n| row id | kind | repo | action | thread | mode | authority | detail | "
        "lane / session | time | evidence |\n|---|---|---|---|---|---|---|---|---|---|---|\n",
        [], EXIT_VIOLATION, "header missing required keys")
    failed = [c for c in cases if not c[3]]
    for label, code, expect, ok, output in cases:
        print("%s  %-58s rc=%d expected=%d" % ("PASS" if ok else "FAIL", label, code, expect))
        if not ok:
            print("      gate output: " + output.strip().replace("\n", "\n      "))
    print("check-contrib-state self-test: %s  cases=%d  failed=%d"
          % ("PASS" if not failed else "FAIL", len(cases), len(failed)))
    return EXIT_PASS if not failed else EXIT_VIOLATION


HEADER_TMPL = (
    "# CONTRIB-STATE - continual contribution mode\n"
    "\n"
    "## Header\n"
    "\n"
    "| key | value |\n"
    "|---|---|\n"
    "| schema_version | 1 |\n"
    "| internal_contrib | %s |\n"
    "| external_contrib | %s |\n"
    "| read_at | 2026-09-17T15:00:00Z |\n"
    "| mode_epoch | - |\n"
    "| last_change | 2026-09-17T15:00:00Z |\n"
)

ROW_HEADER = (
    "\n## Rows\n"
    "\n"
    "| row id | kind | repo | action | thread | mode | authority | detail | lane / session "
    "| time | evidence |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
)

SWITCH_ROW = ("| cc-sw | switch | - | - | - | external | approval op-1 | "
              "external_contrib=on operator reference op-1 | installer | "
              "2026-09-17T09:00:00Z | evidence/x |\n")
ON_STATE = HEADER_TMPL % ("on", "on") + ROW_HEADER + SWITCH_ROW


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.help:
        print(__doc__.strip() if __doc__ else "Usage: check-contrib-state.py [--help]")
        return EXIT_PASS
    if args.self_test:
        return self_test()
    now = parse_iso(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        print("FAIL: --now is not ISO-8601: %s" % args.now)
        return EXIT_INPUT
    write_query = bool(args.action or args.repo)
    target = args.target
    if write_query and target is None:
        print("FAIL: a write query must state --target internal|external "
              "(an unstated target is refused, not guessed)")
        return EXIT_INPUT
    try:
        state = State(resolve_state(args.state or args.record))
        violations = check_shape(state, now)
        if write_query:
            violations += check_write(state, now, target, args.repo, args.action, args.thread)
    except Unusable as exc:
        print("FAIL: %s" % exc)
        return EXIT_INPUT
    return report(state, violations, now, target, args.repo, args.action, args.json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
