#!/usr/bin/env python3
"""idle-watchdog - external 15-minute liveness tick and launch admission gate.

WHAT THIS IS
  A cron entry point. It observes durable state, applies an ordered guard
  pipeline, and - only when every guard is green - launches exactly one
  evaluator sweep. It is an admission gate, not a contribution evaluator: it
  never selects a target, never claims an idle epoch, never performs a closure
  pass, and never writes any shared record other than its own local state,
  event log, and sweep logs.

WHY IT IS SEPARATE FROM THE EVALUATOR
  The canonical contribution decision stays with the evaluator's own locked
  end-of-turn procedure. This process exists only to answer "may one evaluator
  sweep start right now?" and to make the answer auditable. Every skip is
  written to the append-only event log with a concrete reason.

GUARD ORDER (locked; a guard short-circuits and later guards do not run)
  0  configured roots resolve from the environment; no current-directory use
  1  non-blocking advisory flock on var/idle-watchdog.lock
  2  kill-switch parse, immediately after the lock
  3  authoritative process-registry adapter
  4  project state record: section shape, RUNNING rows, AWAITING OWNER blockers
  5  shared state and gate health (inflight, contribution state, rotation)
  6  cooldown and one-fire fingerprint
  7  UTC daily attempt cap
  8  quiet hours, final kill-switch digest recheck, transactional reservation,
     exactly one child launch

FAIL-CLOSED RULES
  Missing, unreadable, malformed, stale, contradictory, or uncertain state is
  always a skip. There is no permissive recovery path. The kill switch is never
  written or flipped by this process. A pending reservation is never cleared by
  age; only the explicit recovery subcommand clears one, and only with an
  operator assertion that no child for that tick is alive.

PROCESS EVIDENCE
  Guard 3 consults the same authoritative registry the evaluator's end-of-turn
  check uses: the profile-scoped background-process registry, whose durable
  form is the checkpoint file `$HERMES_HOME/processes.json`. The adapter below
  reads that checkpoint and emits the fixed probe contract consumed by the
  guard. It is deliberately the only process source: `ps`, `pgrep`, name
  substrings, remembered PIDs, and completion notifications are never used as
  proof. An absent or unparseable registry is uncertainty, not an empty set; a
  valid empty list is the only zero-worker proof.

ENVIRONMENT (all required; the operator sets these in the cron entry)
  PROTEAN_WATCHDOG_ROOT        absolute path to this repository checkout
  PROTEAN_TASK_HOME            absolute path to the project state record
  PROTEAN_CONTRIB_STATE        absolute path to the contribution-state record
  PROTEAN_INFLIGHT             absolute path to the inflight record
  PROTEAN_ROTATION             absolute path to the rotation record
  PROTEAN_HERMES_HOME          absolute path to the evaluator profile home
  PROTEAN_WATCHDOG_PROJECT     project name the tick is scoped to
  PROTEAN_GATE_INFLIGHT        absolute path to the inflight gate script
  PROTEAN_GATE_CONTRIB_STATE   absolute path to the contribution-state gate
  PROTEAN_GATE_ROTATION        absolute path to the rotation gate script
  PROTEAN_SOP_CONTRIB_MODE     absolute path to the locked contribution procedure
  PROTEAN_SOP_IDLE_TRIGGER     absolute path to the locked end-of-turn procedure
  HOME                         absolute path; required by the child environment

SECRETS
  No credential, token, or key is read, copied, printed, logged, or passed to
  the child. The child environment is built from a fixed allowlist, so provider
  credentials are loaded by the child's own profile mechanism and nothing else.

RUNTIME OUTPUT (all under the root)
  var/idle-watchdog.lock          held descriptor; never deleted by age
  records/IDLE-WATCHDOG-STATE.json  atomic admission state
  records/idle-watchdog-ticks.jsonl append-only redacted event rows
  logs/sweeps/<tick-id>.log       child output, mode 0600
  logs/dispatch/<tick-id>.json    non-secret launch diagnostic (provider, model)

SUBCOMMANDS
  (default)                 run one tick
  --process-probe-json      print the process-probe contract; exit 0 when
                            authoritative, 3 when the registry is unusable
  --clear-pending-reservation --tick-id <id> --confirm-no-child-alive
                            bounded operator recovery; refuses without both
                            flags and while the singleton lock is held by a
                            live tick
  --now <RFC3339>           diagnostic instant override used by the tests; the
                            shipped cron line never passes it

EXIT CODES
  0  tick completed (skipped or fired)
  2  the tick could not run at all (unresolvable root or bad arguments)
  3  probe subcommand: registry unusable
  4  the event row failed schema validation (nothing was launched)
  5  an unexpected internal failure (fail closed; nothing was launched or cleared)
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - platform without zoneinfo
    ZoneInfo = None


# --------------------------------------------------------------------------
# Locked constants
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1
COOLDOWN_SECONDS = 7200
DAILY_ATTEMPT_CAP = 4
QUIET_START_HOUR = 5
QUIET_END_HOUR = 21
QUIET_TZ_NAME = "America/New_York"
PROBE_MAX_AGE_SECONDS = 60
PROBE_MAX_FUTURE_SECONDS = 5
GATE_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 30
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PROC_HANDLE_RE = re.compile(r"^proc_[0-9a-f]{12}$")
TICK_ID_RE = re.compile(r"^tick-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
DECISION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
BULLET_RE = re.compile(r"^- ")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

LOCK_REL = os.path.join("var", "idle-watchdog.lock")
STATE_REL = os.path.join("records", "IDLE-WATCHDOG-STATE.json")
EVENT_LOG_REL = os.path.join("records", "idle-watchdog-ticks.jsonl")
RECEIPTS_REL = os.path.join("records", "tick-receipts")
BRIEF_REL = os.path.join("briefs", "idle-watchdog-sweep.md")
SWEEP_LOGS_REL = os.path.join("logs", "sweeps")
DISPATCH_LOGS_REL = os.path.join("logs", "dispatch")
REGISTRY_REL = "processes.json"
PROFILE_CONFIG_REL = "config.yaml"

EVENT_FIELDS = (
    "schema_version",
    "tick_id",
    "at",
    "outcome",
    "dispatch_attempted",
    "reasons",
    "external_contrib",
    "state_md5",
    "task_home_sha256",
    "inflight_md5",
    "rotation_md5",
    "base_fingerprint",
    "worker_count",
    "awaiting_owner_unblocked",
    "gates",
    "daily_attempts_utc",
    "cooldown_until",
    "pid",
)

# Bounded reason vocabulary. A concrete reason may carry a bounded detail after
# the first colon (a field name, gate name, or handle); the base code is fixed.
REASON_CODES = frozenset(
    [
        "singleton-lock-held",
        "kill-switch-off",
        "state-absent",
        "state-unreadable-or-malformed",
        "config-missing",
        "config-not-absolute",
        "process-state-unknown",
        "worker-running",
        "task-home-absent",
        "task-home-unreadable-or-malformed",
        "task-home-running-rows",
        "awaiting-owner-unblocked",
        "awaiting-owner-state-unknown",
        "gate-failed",
        "inflight-malformed-row",
        "inflight-section-missing",
        "contrib-state-illegal",
        "state-changed-during-admission",
        "watchdog-state-absent",
        "watchdog-state-unreadable-or-malformed",
        "pending-reservation",
        "cooldown-active",
        "same-idle-state-already-fired",
        "event-log-absent",
        "event-log-invalid",
        "daily-watchdog-cap",
        "quiet-hours",
        "timezone-unavailable",
        "brief-missing",
        "profile-config-unreadable",
        "hermes-executable-not-found",
        "clock-invalid",
        "state-reservation-failed",
        "launcher-error",
        "internal-error",
        "all-watchdog-guards-green",
        "clock-override-active",
        "operator-cleared-pending-reservation",
    ]
)

ALLOWED_GATE_KEYS = frozenset(["G-6", "G-14", "G-5", "process_probe"])

PATH_ENV_KEYS = (
    ("root", "PROTEAN_WATCHDOG_ROOT"),
    ("task_home", "PROTEAN_TASK_HOME"),
    ("contrib_state", "PROTEAN_CONTRIB_STATE"),
    ("inflight", "PROTEAN_INFLIGHT"),
    ("rotation", "PROTEAN_ROTATION"),
    ("hermes_home", "PROTEAN_HERMES_HOME"),
    ("gate_inflight", "PROTEAN_GATE_INFLIGHT"),
    ("gate_contrib_state", "PROTEAN_GATE_CONTRIB_STATE"),
    ("gate_rotation", "PROTEAN_GATE_ROTATION"),
    ("sop_contrib_mode", "PROTEAN_SOP_CONTRIB_MODE"),
    ("sop_idle_trigger", "PROTEAN_SOP_IDLE_TRIGGER"),
)

SECRET_SHAPE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,}"
    r"|-----BEGIN|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy]|[Tt][Oo][Kk][Ee][Nn]=)"
)


class LogSchemaError(Exception):
    """Raised when an event row would be unsafe or malformed. Nothing launches."""


class TickAbort(Exception):
    """A guard short-circuit. Carries the concrete reason vocabulary entry."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _stderr(message):
    sys.stderr.write("idle-watchdog: %s\n" % message)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt_utc(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text):
    """Parse a `...Z` or offset-bearing RFC-3339 instant; None when invalid."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def md5_bytes(payload):
    return hashlib.md5(payload).hexdigest()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def safe_read_bytes(path):
    try:
        return read_bytes(path), None
    except (OSError, IOError) as exc:
        return None, exc


def ensure_dir(path, mode=0o700):
    if not os.path.isdir(path):
        os.makedirs(path, mode=mode)
    return path


def atomic_write_bytes(path, payload, mode=0o600):
    """Same-directory atomic replace: temp file, fsync, os.replace, dir fsync."""
    directory = os.path.dirname(path) or "."
    ensure_dir(directory)
    fd, tmp = _mkstemp_in(directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _fsync_dir(directory)


def _mkstemp_in(directory):
    for attempt in range(64):
        name = ".idle-watchdog-tmp-%d-%d-%d" % (
            os.getpid(),
            int(time.time() * 1000) % 1000000,
            attempt,
        )
        candidate = os.path.join(directory, name)
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        return fd, candidate
    raise OSError("could not create a temporary file in %s" % directory)


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def canonical_under(path, roots):
    """True when the real path of `path` stays inside one of `roots`."""
    if not os.path.isabs(path):
        return False
    parts = path.split(os.sep)
    if ".." in parts:
        return False
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        try:
            real_root = os.path.realpath(root)
        except OSError:
            continue
        if real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep):
            return True
    return False


# --------------------------------------------------------------------------
# Configuration (guard 0)
# --------------------------------------------------------------------------


class Config(object):
    """Resolved environment surface. Never reads a path out of a state record."""

    def __init__(self, environ):
        self.environ = environ
        self.paths = {}
        self.project = None
        self.home = None
        self.path_env = None

    def raw(self, key):
        env_name = dict(PATH_ENV_KEYS)[key]
        return self.environ.get(env_name, "")

    def require_path(self, key, must_exist=True):
        """Return (path, reason). reason is None on success, else a code."""
        env_name = dict(PATH_ENV_KEYS)[key]
        value = self.environ.get(env_name, "").strip()
        if not value:
            return None, "config-missing:%s" % env_name
        if not os.path.isabs(value):
            return None, "config-not-absolute:%s" % env_name
        if must_exist and not os.path.exists(value):
            return None, "config-missing:%s" % env_name
        return value, None

    def require_home_and_path(self):
        home = (self.environ.get("HOME") or "").strip()
        if not home:
            return "config-missing:HOME"
        if not os.path.isabs(home):
            return "config-not-absolute:HOME"
        path_value = (self.environ.get("PATH") or "").strip()
        if not path_value:
            return "config-missing:PATH"
        return None

    def require_project(self):
        project = (self.environ.get("PROTEAN_WATCHDOG_PROJECT") or "").strip()
        if not project:
            return None, "config-missing:PROTEAN_WATCHDOG_PROJECT"
        if not PROJECT_RE.match(project):
            return None, "config-not-absolute:PROTEAN_WATCHDOG_PROJECT"
        return project, None


# --------------------------------------------------------------------------
# Admission state
# --------------------------------------------------------------------------


def state_path(cfg):
    return os.path.join(cfg.paths["root"], STATE_REL)


def load_state(path):
    """Return (state, reason). A missing or malformed file is a skip."""
    if not os.path.exists(path):
        return None, "watchdog-state-absent"
    try:
        raw = read_bytes(path)
    except (OSError, IOError):
        return None, "watchdog-state-unreadable-or-malformed"
    try:
        state = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "watchdog-state-unreadable-or-malformed"
    if not isinstance(state, dict):
        return None, "watchdog-state-unreadable-or-malformed"
    if set(state.keys()) != set(
        [
            "schema_version",
            "last_dispatch_at",
            "last_attempt_tick_id",
            "last_attempt_base_fingerprint",
            "last_attempt_status",
            "last_fired_base_fingerprint",
            "pending_attempt",
        ]
    ):
        return None, "watchdog-state-unreadable-or-malformed"
    if state.get("schema_version") != SCHEMA_VERSION:
        return None, "watchdog-state-unreadable-or-malformed"
    if state.get("last_attempt_status") not in ("none", "fired", "launcher-error"):
        return None, "watchdog-state-unreadable-or-malformed"
    for field in ("last_attempt_tick_id", "last_attempt_base_fingerprint",
                  "last_fired_base_fingerprint"):
        value = state.get(field)
        if value is not None and not (isinstance(value, str) and HEX64_RE.match(value)):
            if field == "last_attempt_tick_id":
                if not (isinstance(value, str) and TICK_ID_RE.match(value)):
                    return None, "watchdog-state-unreadable-or-malformed"
            else:
                return None, "watchdog-state-unreadable-or-malformed"
    dispatch_at = state.get("last_dispatch_at")
    if dispatch_at is not None and parse_rfc3339(dispatch_at) is None:
        return None, "watchdog-state-unreadable-or-malformed"
    pending = state.get("pending_attempt")
    if pending is not None:
        if not isinstance(pending, dict):
            return None, "watchdog-state-unreadable-or-malformed"
        if set(pending.keys()) != set(
            ["tick_id", "base_fingerprint", "reserved_at", "status"]
        ):
            return None, "watchdog-state-unreadable-or-malformed"
        if pending.get("status") != "reserved":
            return None, "watchdog-state-unreadable-or-malformed"
        if not TICK_ID_RE.match(str(pending.get("tick_id", ""))):
            return None, "watchdog-state-unreadable-or-malformed"
        if not HEX64_RE.match(str(pending.get("base_fingerprint", ""))):
            return None, "watchdog-state-unreadable-or-malformed"
        if parse_rfc3339(pending.get("reserved_at")) is None:
            return None, "watchdog-state-unreadable-or-malformed"
    return state, None


def write_state_atomic(path, state):
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def read_state_back(path, expectation):
    """Read the state file and confirm it matches `expectation` exactly."""
    try:
        observed = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, IOError, ValueError, UnicodeDecodeError):
        return False
    return observed == expectation


# --------------------------------------------------------------------------
# Kill switch (guard 2)
# --------------------------------------------------------------------------


COLON_SWITCH_RE = re.compile(r"^\s*external_contrib\s*:\s*([A-Za-z0-9_\-]+)")


def parse_kill_switch(raw_bytes):
    """Return (value, reason). Only the header field is read; nothing is written."""
    text = raw_bytes.decode("utf-8", "replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = COLON_SWITCH_RE.match(stripped)
        if match:
            value = match.group(1).lower()
            return (value, None) if value in ("on", "off") else (None, "state-unreadable-or-malformed")
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and cells[0] == "external_contrib":
                if len(cells) < 2:
                    return None, "state-unreadable-or-malformed"
                value = cells[1].strip().lower()
                return (value, None) if value in ("on", "off") else (
                    None, "state-unreadable-or-malformed"
                )
    return None, "state-unreadable-or-malformed"


def read_kill_switch(cfg, store):
    """Guard 2. Reads the configured state path only; never accepts a substitute."""
    path = (cfg.environ.get("PROTEAN_CONTRIB_STATE") or "").strip()
    if not path:
        return None, None, "config-missing:PROTEAN_CONTRIB_STATE"
    if not os.path.isabs(path):
        return None, None, "config-not-absolute:PROTEAN_CONTRIB_STATE"
    if not os.path.exists(path):
        return None, None, "state-absent"
    raw, error = safe_read_bytes(path)
    if error is not None:
        return None, None, "state-unreadable-or-malformed"
    digest = md5_bytes(raw)
    store["state_md5"] = digest
    value, reason = parse_kill_switch(raw)
    if reason:
        return None, digest, reason
    store["external_contrib"] = value
    return value, digest, None


# --------------------------------------------------------------------------
# Process-registry adapter (guard 3) and the probe subcommand
# --------------------------------------------------------------------------


def build_probe_envelope(environ, now):
    """Read the profile-scoped registry checkpoint and build the probe contract.

    Returns (envelope, exit_code). The registry checkpoint is the durable form
    of the same background-process registry the evaluator's end-of-turn check
    reads; it lists only processes that have not exited. A missing or
    unparseable checkpoint is uncertainty, never an empty worker set.
    """
    project = (environ.get("PROTEAN_WATCHDOG_PROJECT") or "").strip()
    home = (environ.get("PROTEAN_HERMES_HOME") or "").strip()
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "authoritative": False,
        "generated_at": fmt_utc(now),
        "project": project,
        "workers": [],
    }
    if not home or not os.path.isabs(home) or not project:
        _stderr("probe: PROTEAN_HERMES_HOME or PROTEAN_WATCHDOG_PROJECT unusable")
        return envelope, 3
    checkpoint = os.path.join(home, REGISTRY_REL)
    raw, error = safe_read_bytes(checkpoint)
    if error is not None:
        _stderr("probe: registry checkpoint unreadable: %s" % checkpoint)
        return envelope, 3
    try:
        entries = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _stderr("probe: registry checkpoint is not valid JSON")
        return envelope, 3
    if not isinstance(entries, list):
        _stderr("probe: registry checkpoint is not a list")
        return envelope, 3
    workers = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            _stderr("probe: registry entry is not an object")
            return envelope, 3
        handle = entry.get("session_id")
        if not isinstance(handle, str) or not PROC_HANDLE_RE.match(handle):
            _stderr("probe: registry entry has no usable handle")
            return envelope, 3
        if handle in seen:
            _stderr("probe: duplicate registry handle")
            return envelope, 3
        if not isinstance(entry.get("pid"), int) or not entry.get("pid"):
            _stderr("probe: registry entry has no usable pid")
            return envelope, 3
        if not isinstance(entry.get("task_id"), str) or not entry.get("task_id"):
            _stderr("probe: registry entry is unscoped (no task id)")
            return envelope, 3
        seen.add(handle)
        workers.append({"handle": handle, "state": "running"})
    envelope["workers"] = workers
    envelope["authoritative"] = True
    return envelope, 0


def run_probe_subcommand(now):
    try:
        envelope, code = build_probe_envelope(os.environ, now)
        sys.stdout.write(json.dumps(envelope))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return code
    except Exception as exc:  # pragma: no cover - defensive
        _stderr("probe failed: %s" % exc)
        return 3


def query_process_state(cfg, now, store):
    """Guard 3. Runs the adapter as a subprocess and validates its contract."""
    argv = [sys.executable, os.path.abspath(__file__), "--process-probe-json",
            "--now", fmt_utc(now)]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        store["gates"]["process_probe"] = {"exit": -1}
        return None, "process-state-unknown"
    store["gates"]["process_probe"] = {"exit": completed.returncode}
    if completed.returncode != 0:
        return None, "process-state-unknown"
    try:
        envelope = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "process-state-unknown"
    if not isinstance(envelope, dict):
        return None, "process-state-unknown"
    if set(envelope.keys()) != set(
        ["schema_version", "authoritative", "generated_at", "project", "workers"]
    ):
        return None, "process-state-unknown"
    if envelope.get("schema_version") != SCHEMA_VERSION:
        return None, "process-state-unknown"
    if envelope.get("authoritative") is not True:
        return None, "process-state-unknown"
    if envelope.get("project") != cfg.project:
        return None, "process-state-unknown"
    generated = parse_rfc3339(envelope.get("generated_at"))
    if generated is None:
        return None, "process-state-unknown"
    age = (now - generated).total_seconds()
    if age > PROBE_MAX_AGE_SECONDS or age < -PROBE_MAX_FUTURE_SECONDS:
        return None, "process-state-unknown"
    workers = envelope.get("workers")
    if not isinstance(workers, list):
        return None, "process-state-unknown"
    handles = []
    for worker in workers:
        if not isinstance(worker, dict):
            return None, "process-state-unknown"
        if set(worker.keys()) != set(["handle", "state"]):
            return None, "process-state-unknown"
        handle = worker.get("handle")
        if not isinstance(handle, str) or not PROC_HANDLE_RE.match(handle):
            return None, "process-state-unknown"
        if worker.get("state") != "running":
            return None, "process-state-unknown"
        if handle in handles:
            return None, "process-state-unknown"
        handles.append(handle)
    return handles, None


# --------------------------------------------------------------------------
# Project state record (guard 4)
# --------------------------------------------------------------------------


SECTION_RUNNING = "RUNNING"
SECTION_AWAITING = "AWAITING OWNER"
SECTION_CLOSED = "CLOSED"


def parse_task_home(raw_bytes):
    """Return (parsed, reason). Bytes are read as-is: no line-ending normalisation."""
    text = raw_bytes.decode("utf-8", "replace")
    lines = text.split("\n")
    headings = []
    for index, line in enumerate(lines):
        if line.startswith("## "):
            headings.append((index, line[3:].strip()))
    wanted = {}
    for name in (SECTION_RUNNING, SECTION_AWAITING, SECTION_CLOSED):
        matches = [item for item in headings if item[1] == name]
        if len(matches) != 1:
            return None, "task-home-unreadable-or-malformed"
        wanted[name] = matches[0][0]
    if not (wanted[SECTION_RUNNING] < wanted[SECTION_AWAITING] < wanted[SECTION_CLOSED]):
        return None, "task-home-unreadable-or-malformed"
    starts = [
        (wanted[SECTION_RUNNING], SECTION_RUNNING),
        (wanted[SECTION_AWAITING], SECTION_AWAITING),
        (wanted[SECTION_CLOSED], SECTION_CLOSED),
    ]
    sections = {}
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        sections[name] = lines[start + 1:end]
    return {
        "running_lines": sections[SECTION_RUNNING],
        "awaiting_lines": sections[SECTION_AWAITING],
        "closed_lines": sections[SECTION_CLOSED],
    }, None


def parse_blocker(value):
    """Classify a blocker token: ('artifact', path), ('decision', key), or None.

    The token runs to end-of-line (minus a table-cell terminator): a path may
    contain spaces, so splitting on whitespace is not allowed here.
    """
    token = value.strip().strip("`").strip()
    if not token:
        return None
    if token.startswith("decision:"):
        key = token[len("decision:"):].strip().strip("`").strip()
        if not DECISION_KEY_RE.match(key):
            return None
        return ("decision", key)
    if token.startswith("/"):
        return ("artifact", token)
    return None


def evaluate_awaiting(sections, cfg, store):
    """Return (unblocked_count, reason). Any malformed or uncertain item is a skip."""
    unblocked = 0
    roots = [item for item in (cfg.paths.get("root"), cfg.paths.get("hermes_home")) if item]
    closed_text = "\n".join(sections["closed_lines"])
    contrib_text = ""
    contrib_path = cfg.paths.get("contrib_state")
    if contrib_path and os.path.exists(contrib_path):
        raw, error = safe_read_bytes(contrib_path)
        if error is None:
            contrib_text = raw.decode("utf-8", "replace")
    for line in sections["awaiting_lines"]:
        if not BULLET_RE.match(line):
            continue
        lowered = line
        position = lowered.find("blocked_on:")
        if position < 0:
            return None, "awaiting-owner-state-unknown"
        value = lowered[position + len("blocked_on:"):].strip()
        for terminator in (" |",):
            index = value.find(terminator)
            if index >= 0:
                value = value[:index]
        value = value.strip().strip("`").strip()
        if not value:
            return None, "awaiting-owner-state-unknown"
        parsed = parse_blocker(value)
        if parsed is None:
            return None, "awaiting-owner-state-unknown"
        kind, token = parsed
        if kind == "decision":
            if token in closed_text or token in contrib_text:
                unblocked += 1
            continue
        if not canonical_under(token, roots):
            return None, "awaiting-owner-state-unknown"
        if not os.path.exists(token):
            continue
        if not os.access(token, os.R_OK):
            return None, "awaiting-owner-state-unknown"
        try:
            if os.path.getsize(token) == 0:
                continue
        except OSError:
            return None, "awaiting-owner-state-unknown"
        unblocked += 1
    return unblocked, None


def read_task_home(cfg, store):
    """Guard 4. Reads the configured project record as bytes."""
    path = (cfg.environ.get("PROTEAN_TASK_HOME") or "").strip()
    if not path:
        return None, "config-missing:PROTEAN_TASK_HOME"
    if not os.path.isabs(path):
        return None, "config-not-absolute:PROTEAN_TASK_HOME"
    if not os.path.exists(path):
        return None, "task-home-absent"
    raw, error = safe_read_bytes(path)
    if error is not None:
        return None, "task-home-unreadable-or-malformed"
    store["task_home_sha256"] = sha256_bytes(raw)
    sections, reason = parse_task_home(raw)
    if reason:
        return None, reason
    running_rows = len([line for line in sections["running_lines"] if BULLET_RE.match(line)])
    if running_rows > 0:
        return None, "task-home-running-rows:%d" % running_rows
    unblocked, reason = evaluate_awaiting(sections, cfg, store)
    if reason:
        return None, reason
    store["awaiting_owner_unblocked"] = unblocked
    if unblocked > 0:
        return None, "awaiting-owner-unblocked:%d" % unblocked
    return sections, None


# --------------------------------------------------------------------------
# Shared state and gate health (guard 5)
# --------------------------------------------------------------------------


def run_gate(argv, store, name):
    """Run one read-only gate. Returns (stdout_text, reason)."""
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        store["gates"][name] = {"exit": -1, "error": exc.__class__.__name__}
        return None, "gate-failed:%s" % name
    output = completed.stdout.decode("utf-8", "replace")
    store["gates"][name] = {"exit": completed.returncode}
    if completed.returncode != 0:
        return output, "gate-failed:%s" % name
    for line in output.splitlines():
        if line.strip().startswith("FAIL:"):
            store["gates"][name]["fail_lines"] = 1
            return output, "gate-failed:%s" % name
    return output, None


INFLIGHT_SECTION_RE = re.compile(r"^##+\s+Lease rows\s*$")
SEPARATOR_RE = re.compile(r"^:?-{2,}:?$")


def validate_inflight_rows(text):
    """Independent 11-cell validation; the gate summary code is not trusted."""
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        if INFLIGHT_SECTION_RE.match(line.strip()):
            start = index
    if start is None:
        return None, "inflight-section-missing"
    rows = 0
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(SEPARATOR_RE.match(cell) for cell in cells if cell):
            continue
        if cells and cells[0] in ("claim id", "column"):
            continue
        rows += 1
        if len(cells) != 11:
            return None, "inflight-malformed-row:%d-cells" % len(cells)
    return rows, None


def read_shared_state(cfg, store):
    """Guard 5. Read-only gates plus an independent record-shape check."""
    paths = {}
    for key in ("inflight", "rotation", "contrib_state",
                "gate_inflight", "gate_contrib_state", "gate_rotation"):
        value, reason = cfg.require_path(key)
        if reason:
            return None, reason
        paths[key] = value

    raw, error = safe_read_bytes(paths["inflight"])
    if error is not None:
        return None, "config-missing:PROTEAN_INFLIGHT"
    store["inflight_md5"] = md5_bytes(raw)

    raw, error = safe_read_bytes(paths["rotation"])
    if error is not None:
        return None, "config-missing:PROTEAN_ROTATION"
    store["rotation_md5"] = md5_bytes(raw)

    _, reason = run_gate(
        [sys.executable, paths["gate_inflight"], paths["inflight"]], store, "G-6"
    )
    if reason:
        return None, reason

    inflight_text = paths["inflight"]
    rows, reason = validate_inflight_rows(
        read_bytes(inflight_text).decode("utf-8", "replace")
    )
    if reason:
        return None, reason
    store["gates"]["G-6"]["rows"] = rows

    output, reason = run_gate(
        [sys.executable, paths["gate_contrib_state"],
         "--state", paths["contrib_state"], "--json"],
        store, "G-14",
    )
    if reason:
        return None, reason
    try:
        parsed = json.loads(output)
    except ValueError:
        return None, "contrib-state-illegal:unparseable-json"
    if not isinstance(parsed, dict):
        return None, "contrib-state-illegal:unparseable-json"
    internal = parsed.get("internal_contrib")
    external = parsed.get("external_contrib")
    store["gates"]["G-14"]["internal_contrib"] = internal
    store["gates"]["G-14"]["external_contrib"] = external
    if internal != "on":
        return None, "contrib-state-illegal:internal-not-on"
    if external != "on":
        return None, "contrib-state-illegal:external-not-on"

    _, reason = run_gate(
        [sys.executable, paths["gate_rotation"], paths["rotation"]], store, "G-5"
    )
    if reason:
        return None, reason

    raw, error = safe_read_bytes(paths["contrib_state"])
    if error is not None:
        return None, "state-changed-during-admission"
    if md5_bytes(raw) != store.get("state_md5"):
        return None, "state-changed-during-admission"
    return paths, None


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------


def event_log_path(cfg):
    return os.path.join(cfg.paths["root"], EVENT_LOG_REL)


def validate_row(row):
    if not isinstance(row, dict):
        raise LogSchemaError("row is not an object")
    if set(row.keys()) != set(EVENT_FIELDS):
        raise LogSchemaError("row field set is not the fixed schema")
    if row.get("schema_version") != SCHEMA_VERSION:
        raise LogSchemaError("schema_version is not %d" % SCHEMA_VERSION)
    if row.get("outcome") not in ("fired", "skipped"):
        raise LogSchemaError("outcome is not fired or skipped")
    if not isinstance(row.get("dispatch_attempted"), bool):
        raise LogSchemaError("dispatch_attempted is not a boolean")
    reasons = row.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        raise LogSchemaError("row has no reason")
    for reason in reasons:
        if not isinstance(reason, str) or not reason:
            raise LogSchemaError("reason is not a non-empty string")
        if reason.split(":")[0] not in REASON_CODES:
            raise LogSchemaError("reason is outside the bounded vocabulary: %s" % reason)
    if row.get("external_contrib") not in ("on", "off", None):
        raise LogSchemaError("external_contrib is outside the bounded vocabulary")
    if row.get("worker_count") is not None and not isinstance(row["worker_count"], int):
        raise LogSchemaError("worker_count is not an integer or null")
    if row.get("awaiting_owner_unblocked") is not None and not isinstance(
        row["awaiting_owner_unblocked"], int
    ):
        raise LogSchemaError("awaiting_owner_unblocked is not an integer or null")
    if row.get("daily_attempts_utc") is not None and not isinstance(
        row["daily_attempts_utc"], int
    ):
        raise LogSchemaError("daily_attempts_utc is not an integer or null")
    if row.get("pid") is not None and not isinstance(row["pid"], int):
        raise LogSchemaError("pid is not an integer or null")
    gates = row.get("gates")
    if not isinstance(gates, dict):
        raise LogSchemaError("gates is not an object")
    for key, value in gates.items():
        if key not in ALLOWED_GATE_KEYS:
            raise LogSchemaError("unknown gate key: %s" % key)
        if not isinstance(value, dict):
            raise LogSchemaError("gate entry is not an object")
    for field in ("state_md5", "task_home_sha256", "inflight_md5", "rotation_md5"):
        value = row.get(field)
        if value is not None and not (isinstance(value, str) and re.match(r"^[0-9a-f]{32,64}$", value)):
            raise LogSchemaError("%s is not a digest" % field)
    base = row.get("base_fingerprint")
    if base is not None and not (isinstance(base, str) and HEX64_RE.match(base)):
        raise LogSchemaError("base_fingerprint is not a sha256")
    for field in ("at", "cooldown_until"):
        value = row.get(field)
        if value is not None and parse_rfc3339(value) is None:
            raise LogSchemaError("%s is not RFC-3339 UTC" % field)
    if not TICK_ID_RE.match(str(row.get("tick_id", ""))):
        raise LogSchemaError("tick_id is not tick-shaped")
    serialized = json.dumps(row)
    if SECRET_SHAPE_RE.search(serialized):
        raise LogSchemaError("row looks like it carries a secret-shaped value")
    return True


def append_event(cfg, row):
    """Append one validated row with a single O_APPEND write, then sync."""
    validate_row(row)
    path = event_log_path(cfg)
    ensure_dir(os.path.dirname(path))
    payload = (json.dumps(row, sort_keys=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise LogSchemaError("short append")
        os.fsync(fd)
    finally:
        os.close(fd)


def load_event_rows(cfg):
    """Return (rows, reason). Every non-empty line must be a valid row."""
    path = event_log_path(cfg)
    if not os.path.exists(path):
        return None, "event-log-absent"
    raw, error = safe_read_bytes(path)
    if error is not None:
        return None, "event-log-invalid"
    text = raw.decode("utf-8", "replace")
    rows = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return None, "event-log-invalid"
        try:
            validate_row(row)
        except LogSchemaError:
            return None, "event-log-invalid"
        rows.append(row)
    return rows, None


# --------------------------------------------------------------------------
# Fingerprints, cooldown, cap, quiet hours
# --------------------------------------------------------------------------


def base_fingerprint(task_home_sha256, inflight_md5, rotation_md5):
    payload = (
        task_home_sha256.encode("ascii")
        + inflight_md5.encode("ascii")
        + rotation_md5.encode("ascii")
    )
    return sha256_bytes(payload)


def local_hour(now):
    """Return (hour, reason). A missing timezone database is a fail-closed skip."""
    if ZoneInfo is None:
        return None, "timezone-unavailable"
    try:
        zone = ZoneInfo(QUIET_TZ_NAME)
    except Exception:
        return None, "timezone-unavailable"
    try:
        return now.astimezone(zone).hour, None
    except Exception:
        return None, "timezone-unavailable"


# --------------------------------------------------------------------------
# Profile configuration and launcher
# --------------------------------------------------------------------------


CONFIG_PROVIDER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def read_profile_diagnostic(cfg):
    """Return (diagnostic, reason). Only two scalars are read, never a value copy."""
    home = cfg.paths.get("hermes_home")
    if not home:
        return None, "config-missing:PROTEAN_HERMES_HOME"
    path = os.path.join(home, PROFILE_CONFIG_REL)
    if not os.path.exists(path):
        return None, "config-missing:PROTEAN_HERMES_HOME"
    raw, error = safe_read_bytes(path)
    if error is not None:
        return None, "profile-config-unreadable"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "profile-config-unreadable"
    provider = None
    model_default = None
    in_model = False
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            return None, "profile-config-unreadable"
        indent = len(line) - len(line.lstrip(" "))
        match = CONFIG_PROVIDER_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip().strip("'\"")
        if indent == 0:
            in_model = key == "model"
            if key == "provider" and value:
                provider = value
            continue
        if in_model and key == "default" and value:
            model_default = value
    if not provider or not model_default:
        return None, "profile-config-unreadable"
    return {"provider": provider, "model": model_default}, None


def resolve_hermes_bin(cfg):
    """Resolve the child executable from the child's own PATH, never the
    process default: the cron environment is the only PATH that matters."""
    env_name = (cfg.environ.get("PROTEAN_WATCHDOG_HERMES_BIN") or "").strip()
    if env_name:
        if os.path.isabs(env_name) and os.access(env_name, os.X_OK):
            return env_name, None
        return None, "hermes-executable-not-found"
    search_path = (cfg.environ.get("PATH") or "").strip()
    if not search_path:
        return None, "hermes-executable-not-found"
    found = shutil.which("hermes", path=search_path)
    if not found:
        return None, "hermes-executable-not-found"
    return found, None


def build_child_env(cfg, tick_id, base, receipt, brief_path):
    """Fixed allowlist. No credential, token, or key can reach the child."""
    return {
        "HOME": cfg.home,
        "PATH": cfg.path_env,
        "HERMES_HOME": cfg.paths["hermes_home"],
        "PROTEAN_WATCHDOG_ROOT": cfg.paths["root"],
        "PROTEAN_TASK_HOME": cfg.paths["task_home"],
        "PROTEAN_CONTRIB_STATE": cfg.paths["contrib_state"],
        "PROTEAN_WATCHDOG_TICK_ID": tick_id,
        "PROTEAN_WATCHDOG_BASE_FINGERPRINT": base,
        "PROTEAN_WATCHDOG_RECEIPT": receipt,
        "PROTEAN_SOP_CONTRIB_MODE": cfg.paths["sop_contrib_mode"],
        "PROTEAN_SOP_IDLE_TRIGGER": cfg.paths["sop_idle_trigger"],
    }


def launch_child(cfg, argv, env, sweep_log):
    """Start exactly one child. A successful spawn is the only dispatch point."""
    ensure_dir(os.path.dirname(sweep_log))
    fd = os.open(sweep_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    handle = os.fdopen(fd, "ab")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cfg.paths["root"],
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        handle.close()
        raise
    handle.close()
    return process


# --------------------------------------------------------------------------
# Tick
# --------------------------------------------------------------------------


class Tick(object):
    """Accumulates the fixed event row for one tick."""

    def __init__(self, cfg, tick_id, now, overridden=False):
        self.cfg = cfg
        self.tick_id = tick_id
        self.now = now
        self.row: Dict[str, Any] = dict((field, None) for field in EVENT_FIELDS)
        self.row["schema_version"] = SCHEMA_VERSION
        self.row["tick_id"] = tick_id
        self.row["at"] = fmt_utc(now)
        self.row["outcome"] = "skipped"
        self.row["dispatch_attempted"] = False
        self.row["reasons"] = []
        self.row["gates"] = {}
        if overridden:
            self.row["reasons"].append("clock-override-active")

    def note(self, reason):
        if reason and reason not in self.row["reasons"]:
            self.row["reasons"].append(reason)

    def emit(self):
        if not self.row["reasons"]:
            self.row["reasons"] = ["all-watchdog-guards-green"]
        append_event(self.cfg, self.row)


def new_tick_id(now):
    suffix = hashlib.sha256(
        ("%s-%d-%d" % (fmt_utc(now), os.getpid(), int(time.time() * 1000))).encode("utf-8")
    ).hexdigest()[:8]
    return "tick-%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), suffix)


def run_tick(cfg, now, overridden):
    """Run the ordered guard pipeline. Returns the process exit code."""
    tick_id = new_tick_id(now)
    tick = Tick(cfg, tick_id, now, overridden=overridden)
    row = tick.row

    lock_path = os.path.join(cfg.paths["root"], LOCK_REL)
    ensure_dir(os.path.dirname(lock_path))

    # Guard 1 - singleton lock (non-blocking; never wait, never unlink by age).
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            tick.note("singleton-lock-held")
            try:
                tick.emit()
            except LogSchemaError as exc:
                _stderr("collision row rejected: %s" % exc)
            return 0

        # The descriptor is held from here through the final write.
        os.ftruncate(lock_fd, 0)
        os.write(
            lock_fd,
            ("pid=%d tick=%s at=%s\n" % (os.getpid(), tick_id, fmt_utc(now))).encode("utf-8"),
        )
        os.fsync(lock_fd)

        outcome = _guarded_tick(cfg, tick, lock_fd)
        if outcome is not None:
            return outcome
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        os.close(lock_fd)


def _guarded_tick(cfg, tick, lock_fd):
    """Guard 2 onward. Returns an exit code to abort, or None for a normal tick."""
    row = tick.row
    now = tick.now
    store = {"gates": row["gates"]}

    # Guard 2 - kill switch, immediately after the lock.
    try:
        value, _, reason = read_kill_switch(cfg, store)
        row["state_md5"] = store.get("state_md5")
        row["external_contrib"] = store.get("external_contrib")
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        if value == "off":
            tick.note("kill-switch-off")
            tick.emit()
            return 0
    except LogSchemaError as exc:
        _stderr("event row rejected: %s" % exc)
        return 4

    try:
        # Guard 3 - authoritative process registry.
        handles, reason = query_process_state(cfg, now, store)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        row["worker_count"] = len(handles)
        if handles:
            tick.note("worker-running:%s" % handles[0])
            tick.emit()
            return 0

        # Guard 4 - project state record.
        _, reason = read_task_home(cfg, store)
        row["task_home_sha256"] = store.get("task_home_sha256")
        row["awaiting_owner_unblocked"] = store.get("awaiting_owner_unblocked")
        if reason:
            tick.note(reason)
            tick.emit()
            return 0

        # Guard 5 - shared state and gate health.
        _, reason = read_shared_state(cfg, store)
        row["inflight_md5"] = store.get("inflight_md5")
        row["rotation_md5"] = store.get("rotation_md5")
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        fp = base_fingerprint(
            row["task_home_sha256"], row["inflight_md5"], row["rotation_md5"]
        )
        row["base_fingerprint"] = fp

        # Guard 6 - cooldown, pending reservation, and the one-fire fingerprint.
        state, reason = load_state(state_path(cfg))
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        pending = state.get("pending_attempt")
        if pending is not None:
            tick.note("pending-reservation:%s" % pending["tick_id"])
            tick.emit()
            return 0
        if state.get("last_dispatch_at"):
            last = parse_rfc3339(state["last_dispatch_at"])
            cooldown_until = last + timedelta(seconds=COOLDOWN_SECONDS)
            row["cooldown_until"] = fmt_utc(cooldown_until)
            if now < cooldown_until:
                tick.note("cooldown-active")
                tick.emit()
                return 0
        if state.get("last_fired_base_fingerprint") == fp:
            tick.note("same-idle-state-already-fired")
            tick.emit()
            return 0

        # Guard 7 - UTC daily attempt cap, derived from the redacted event log.
        rows, reason = load_event_rows(cfg)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        today = fmt_utc(now)[:10]
        attempts = len(
            [
                item
                for item in rows
                if item.get("dispatch_attempted") is True
                and str(item.get("at", ""))[:10] == today
            ]
        )
        row["daily_attempts_utc"] = attempts
        if attempts >= DAILY_ATTEMPT_CAP:
            tick.note("daily-watchdog-cap:%d" % attempts)
            tick.emit()
            return 0

        # Guard 8 - quiet hours, final toggle recheck, reservation, one launch.
        hour, reason = local_hour(now)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        if not (QUIET_START_HOUR <= hour < QUIET_END_HOUR):
            tick.note("quiet-hours:%02d" % hour)
            tick.emit()
            return 0

        _, _, reason = read_kill_switch(cfg, store)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        if store.get("external_contrib") != "on":
            tick.note("state-changed-during-admission")
            tick.emit()
            return 0

        brief_path = os.path.join(cfg.paths["root"], BRIEF_REL)
        if not os.path.exists(brief_path):
            tick.note("brief-missing")
            tick.emit()
            return 0
        for key in ("sop_contrib_mode", "sop_idle_trigger"):
            _, reason = cfg.require_path(key)
            if reason:
                tick.note(reason)
                tick.emit()
                return 0

        diagnostic, reason = read_profile_diagnostic(cfg)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        hermes_bin, reason = resolve_hermes_bin(cfg)
        if reason:
            tick.note(reason)
            tick.emit()
            return 0
        reason = cfg.require_home_and_path()
        if reason:
            tick.note(reason)
            tick.emit()
            return 0

        # Transactional reservation: state first, verified by read-back.
        receipt = os.path.join(cfg.paths["root"], RECEIPTS_REL, "%s.md" % tick.tick_id)
        reserved_at = fmt_utc(now)
        reserved_state = dict(state)
        reserved_state["pending_attempt"] = {
            "tick_id": tick.tick_id,
            "base_fingerprint": fp,
            "reserved_at": reserved_at,
            "status": "reserved",
        }
        try:
            write_state_atomic(state_path(cfg), reserved_state)
        except OSError:
            tick.note("state-reservation-failed")
            tick.emit()
            return 0
        if not read_state_back(state_path(cfg), reserved_state):
            tick.note("state-reservation-failed")
            tick.emit()
            return 0

        sweep_log = os.path.join(
            cfg.paths["root"], SWEEP_LOGS_REL, "%s.log" % tick.tick_id
        )
        env = build_child_env(cfg, tick.tick_id, fp, receipt, brief_path)
        argv = [hermes_bin, "chat", "--oneshot", "--query-file", brief_path]
        diagnostic.update(
            {
                "tick_id": tick.tick_id,
                "at": fmt_utc(now),
                "argv": argv,
                "base_fingerprint": fp,
                "receipt": receipt,
                "sweep_log": sweep_log,
            }
        )
        try:
            ensure_dir(os.path.join(cfg.paths["root"], DISPATCH_LOGS_REL))
            atomic_write_bytes(
                os.path.join(
                    cfg.paths["root"], DISPATCH_LOGS_REL, "%s.json" % tick.tick_id
                ),
                (json.dumps(diagnostic, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        except OSError:
            tick.note("state-reservation-failed")
            tick.emit()
            return 0

        pid = None
        try:
            process = launch_child(cfg, argv, env, sweep_log)
            pid = process.pid
            if not isinstance(pid, int) or pid <= 0:
                pid = None
        except Exception:
            pid = None

        if pid is None:
            failed_state = dict(reserved_state)
            failed_state["pending_attempt"] = None
            failed_state["last_dispatch_at"] = reserved_at
            failed_state["last_attempt_tick_id"] = tick.tick_id
            failed_state["last_attempt_base_fingerprint"] = fp
            failed_state["last_attempt_status"] = "launcher-error"
            try:
                write_state_atomic(state_path(cfg), failed_state)
            except OSError:
                pass
            row["outcome"] = "skipped"
            row["dispatch_attempted"] = True
            row["cooldown_until"] = fmt_utc(
                now + timedelta(seconds=COOLDOWN_SECONDS)
            )
            tick.note("launcher-error")
            tick.emit()
            return 0

        fired_state = dict(reserved_state)
        fired_state["pending_attempt"] = None
        fired_state["last_dispatch_at"] = reserved_at
        fired_state["last_attempt_tick_id"] = tick.tick_id
        fired_state["last_attempt_base_fingerprint"] = fp
        fired_state["last_attempt_status"] = "fired"
        fired_state["last_fired_base_fingerprint"] = fp
        try:
            write_state_atomic(state_path(cfg), fired_state)
        except OSError:
            tick.note("state-reservation-failed")
            tick.emit()
            return 0
        if not read_state_back(state_path(cfg), fired_state):
            tick.note("state-reservation-failed")
            tick.emit()
            return 0

        row["outcome"] = "fired"
        row["dispatch_attempted"] = True
        row["pid"] = pid
        row["cooldown_until"] = fmt_utc(now + timedelta(seconds=COOLDOWN_SECONDS))
        tick.note("all-watchdog-guards-green")
        tick.emit()
        return 0
    except LogSchemaError as exc:
        _stderr("event row rejected: %s" % exc)
        return 4
    except Exception as exc:  # fail closed: nothing cleared, nothing recovered
        _stderr("unexpected failure: %s: %s" % (exc.__class__.__name__, exc))
        try:
            tick.note("internal-error")
            tick.emit()
        except Exception:
            pass
        return 5


def run_recovery(cfg, tick_id, confirmed):
    """Bounded operator recovery for a stranded reservation. Refuses otherwise."""
    lock_path = os.path.join(cfg.paths["root"], LOCK_REL)
    ensure_dir(os.path.dirname(lock_path))
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            _stderr("recovery refused: the singleton lock is held by a live tick")
            return 1
        if not confirmed:
            _stderr("recovery refused: --confirm-no-child-alive is required")
            return 1
        path = state_path(cfg)
        state, reason = load_state(path)
        if reason:
            _stderr("recovery refused: %s" % reason)
            return 1
        pending = state.get("pending_attempt")
        if pending is None:
            _stderr("recovery refused: there is no pending reservation")
            return 1
        if pending["tick_id"] != tick_id:
            _stderr("recovery refused: --tick-id does not match the pending reservation")
            return 1
        now = utc_now()
        updated = dict(state)
        updated["pending_attempt"] = None
        updated["last_attempt_tick_id"] = pending["tick_id"]
        updated["last_attempt_base_fingerprint"] = pending["base_fingerprint"]
        updated["last_attempt_status"] = "launcher-error"
        updated["last_dispatch_at"] = pending["reserved_at"]
        write_state_atomic(path, updated)
        if not read_state_back(path, updated):
            _stderr("recovery failed: state read-back did not match")
            return 1
        tick = Tick(cfg, new_tick_id(now), now)
        tick.row["cooldown_until"] = fmt_utc(
            parse_rfc3339(pending["reserved_at"]) + timedelta(seconds=COOLDOWN_SECONDS)
        )
        tick.note("operator-cleared-pending-reservation:%s" % pending["tick_id"])
        tick.emit()
        sys.stdout.write("cleared pending reservation %s\n" % pending["tick_id"])
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        os.close(lock_fd)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="idle-watchdog.py",
        description="15-minute liveness tick and launch admission gate.",
    )
    parser.add_argument("--process-probe-json", action="store_true",
                        help="print the process-probe contract and exit")
    parser.add_argument("--clear-pending-reservation", action="store_true",
                        help="bounded operator recovery for a stranded reservation")
    parser.add_argument("--tick-id", default=None,
                        help="the tick id the recovery targets")
    parser.add_argument("--confirm-no-child-alive", action="store_true",
                        help="operator assertion required by recovery")
    parser.add_argument("--now", default=None,
                        help="diagnostic RFC-3339 instant override (tests only)")
    return parser.parse_args(argv)


def effective_now(raw):
    if not raw:
        return utc_now(), False, None
    if not RFC3339_RE.match(raw.strip()):
        return None, False, "clock-invalid"
    parsed = parse_rfc3339(raw)
    if parsed is None:
        return None, False, "clock-invalid"
    return parsed.replace(microsecond=0), True, None


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    os.umask(0o077)

    now, overridden, reason = effective_now(args.now)
    if reason:
        _stderr("bad --now value")
        return 2

    if args.process_probe_json:
        return run_probe_subcommand(now)

    cfg = Config(os.environ)
    root, reason = cfg.require_path("root", must_exist=False)
    if reason:
        _stderr("cannot resolve PROTEAN_WATCHDOG_ROOT: %s" % reason)
        return 2
    if not os.path.isdir(root):
        _stderr("PROTEAN_WATCHDOG_ROOT is not a directory: %s" % root)
        return 2
    cfg.paths["root"] = root
    project, reason = cfg.require_project()
    if reason:
        _stderr("cannot resolve PROTEAN_WATCHDOG_PROJECT: %s" % reason)
        return 2
    cfg.project = project
    cfg.home = (os.environ.get("HOME") or "").strip()
    cfg.path_env = (os.environ.get("PATH") or "").strip()

    for key, _ in PATH_ENV_KEYS:
        if key == "root":
            continue
        value = (os.environ.get(dict(PATH_ENV_KEYS)[key]) or "").strip()
        if value and os.path.isabs(value):
            cfg.paths[key] = value

    ensure_dir(os.path.join(root, "var"))
    ensure_dir(os.path.join(root, "logs"))
    ensure_dir(os.path.join(root, "logs", "sweeps"))
    ensure_dir(os.path.join(root, "logs", "dispatch"))
    ensure_dir(os.path.join(root, "records", "tick-receipts"))

    if args.clear_pending_reservation:
        if not args.tick_id:
            _stderr("recovery refused: --tick-id is required")
            return 1
        return run_recovery(cfg, args.tick_id, args.confirm_no_child_alive)

    return run_tick(cfg, now, overridden)


if __name__ == "__main__":
    sys.exit(main())
