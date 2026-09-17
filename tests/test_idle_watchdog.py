#!/usr/bin/env python3
"""Focused fixture and subprocess tests for the idle watchdog admission gate.

Every case runs inside a temporary root. No test touches a live project record,
a live profile database, a live gate, or a real evaluator session, and no test
changes the real contribution-state record. The launcher is a fake executable
that records its own argv and environment, so child invocation counts and the
launched environment are asserted directly rather than inferred.

Run: python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "idle-watchdog.py"
WATCHDOG = [sys.executable, str(SCRIPT)]

PROJECT = "demoproject"


def _green_quiet_times():
    """Pick a green (inside-window) and a quiet (outside-window) UTC instant.

    Both sit on the current UTC calendar day so the daily attempt cap guard,
    which compares event rows against the tick's own effective date, stays
    consistent with the fabricated cooldown timestamps the tests write.
    """
    now = datetime.now(timezone.utc)
    zone = None
    if ZoneInfo is not None:
        try:
            zone = ZoneInfo("America/New_York")
        except Exception:
            zone = None

    def is_green(utc_hour):
        if zone is None:
            return 9 <= utc_hour < 21  # EDT-equivalent guess
        local = now.replace(hour=utc_hour, minute=0, second=0,
                            microsecond=0).astimezone(zone)
        return 5 <= local.hour < 21

    candidates = [h for h in range(3, 22) if is_green(h)] or [15]
    green_hour = candidates[len(candidates) // 2]
    quiet_hour = next((h for h in range(24) if not is_green(h)), 3)

    def fmt(moment):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    green = now.replace(hour=green_hour, minute=45, second=0, microsecond=0)
    return {
        "green": fmt(green),
        "green_next": fmt(green + timedelta(minutes=5)),
        "quiet": fmt(now.replace(hour=quiet_hour, minute=15, second=0,
                                 microsecond=0)),
        "cooldown_start": fmt(green - timedelta(minutes=1)),
        "cooldown_until": fmt(green + timedelta(hours=2) - timedelta(minutes=1)),
        "cooldown_expired": fmt(green - timedelta(hours=3)),
        "green_slots": [fmt(now.replace(hour=h, minute=45, second=0, microsecond=0))
                        for h in candidates],
    }


TIMES = _green_quiet_times()
NOW_GREEN = TIMES["green"]
NOW_QUIET = TIMES["quiet"]

def _shift(instant, minutes=0, hours=0):
    moment = datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc) + timedelta(minutes=minutes, hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tick_id(instant):
    moment = datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ")
    return "tick-%s-%s" % (moment.strftime("%Y%m%d-%H%M%S"), "a" * 8)

SEED_STATE = {
    "schema_version": 1,
    "last_dispatch_at": None,
    "last_attempt_tick_id": None,
    "last_attempt_base_fingerprint": None,
    "last_attempt_status": "none",
    "last_fired_base_fingerprint": None,
    "pending_attempt": None,
}

EVENT_FIELDS = [
    "schema_version", "tick_id", "at", "outcome", "dispatch_attempted", "reasons",
    "external_contrib", "state_md5", "task_home_sha256", "inflight_md5",
    "rotation_md5", "base_fingerprint", "worker_count", "awaiting_owner_unblocked",
    "gates", "daily_attempts_utc", "cooldown_until", "pid",
]

INFLIGHT_HEADER = (
    "# INFLIGHT - live writer leases\n\n"
    "## Lease rows\n\n"
    "| claim id | session id | role | provider/model | tree / worktree / branch "
    "| owned files | claim time | lease expiry | release time | conflict result | status |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| (empty state) | - | - | - | - | - | - | - | - | - | empty, no live writer |\n"
)

CONTRIB_HEADER = (
    "# CONTRIB-STATE - contribution mode\n\n"
    "## Header (fixed)\n\n"
    "| key | value |\n|---|---|\n"
    "| schema_version | 1 |\n"
    "| internal_contrib | on |\n"
    "| external_contrib | {external} |\n"
)


def gate_json(internal="on", external="on"):
    return json.dumps(
        {
            "gate": "G-14",
            "verdict": "PASS",
            "internal_contrib": internal,
            "external_contrib": external,
            "violations": [],
        }
    )


class Harness(object):
    """Builds one temporary root and the fake executables the tick will call."""

    def __init__(self, space=False, name="idle-watchdog-case"):
        base = tempfile.mkdtemp(prefix=name + "-")
        if space:
            base = os.path.join(base, "root with a space")
            os.makedirs(base)
        self.base = base
        self.ops = os.path.join(self.base, "ops")
        self.evalhome = os.path.join(self.base, "eval-home")
        self.bin = os.path.join(self.base, "bin")
        for path in (
            self.ops,
            os.path.join(self.ops, "records"),
            os.path.join(self.ops, "briefs"),
            os.path.join(self.ops, "var"),
            os.path.join(self.ops, "logs"),
            self.evalhome,
            self.bin,
        ):
            os.makedirs(path, exist_ok=True)
        self.task_home = os.path.join(self.base, "task-home.md")
        self.contrib = os.path.join(self.base, "contrib-state.md")
        self.inflight = os.path.join(self.base, "inflight.md")
        self.rotation = os.path.join(self.base, "rotation-state.md")
        self.sop_contrib = os.path.join(self.base, "procedure-contribution.md")
        self.sop_eot = os.path.join(self.base, "procedure-end-of-turn.md")
        self.state_path = os.path.join(self.ops, "records", "IDLE-WATCHDOG-STATE.json")
        self.log_path = os.path.join(self.ops, "records", "idle-watchdog-ticks.jsonl")
        self.registry = os.path.join(self.evalhome, "processes.json")
        self.child_record = os.path.join(self.base, "child-invocations.jsonl")

        self.write(os.path.join(self.ops, "briefs", "idle-watchdog-sweep.md"),
                   "# sweep brief\n\nsynthetic brief for the fixture root.\n")
        self.write(self.sop_contrib, "locked contribution procedure (fixture)\n")
        self.write(self.sop_eot, "locked end-of-turn procedure (fixture)\n")
        self.write(self.inflight, INFLIGHT_HEADER)
        self.write(self.rotation, "# ROTATION-STATE\n\n## Rows\n\n- none\n")
        self.write(self.log_path, "")
        self.write(self.state_path, json.dumps(SEED_STATE, indent=2) + "\n")
        self.write(self.evalhome + "/config.yaml",
                   "provider: fixture-provider\nmodel:\n  default: fixture-model\n")
        self.write(self.registry, "[]\n")
        self.write_task_home()
        self.write_contrib(True, "on", "on")
        self.write_gates()
        self.write_fake_hermes()

    # -- file helpers ----------------------------------------------------
    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_task_home(self, running_rows=0, awaiting_rows=None, closed_text=""):
        if awaiting_rows is None:
            awaiting_rows = ["- item one: blocked_on: %s" % os.path.join(self.ops, "absent.md")]
        lines = ["# PROJECT STATE", "", "## RUNNING", ""]
        for index in range(running_rows):
            lines.append("- lane %d: handle proc_%012x" % (index, index))
        lines += ["", "## AWAITING OWNER", ""] + list(awaiting_rows)
        lines += ["", "## CLOSED", ""]
        if closed_text:
            lines.append(closed_text)
        lines.append("")
        return self.write(self.task_home, "\n".join(lines))

    def write_contrib(self, present=True, internal="on", external="on", body=""):
        if not present:
            if os.path.exists(self.contrib):
                os.unlink(self.contrib)
            return None
        text = CONTRIB_HEADER.format(external=external)
        if internal != "on":
            text = text.replace("| internal_contrib | on |",
                                "| internal_contrib | %s |" % internal)
        return self.write(self.contrib, text + body)

    def write_task_home_raw(self, text):
        return self.write(self.task_home, text)

    def write_inflight_raw(self, text):
        return self.write(self.inflight, text)

    def write_state(self, state):
        return self.write(self.state_path, json.dumps(state, indent=2) + "\n")

    def write_registry(self, entries):
        return self.write(self.registry, json.dumps(entries, indent=2) + "\n")

    def remove_registry(self):
        if os.path.exists(self.registry):
            os.unlink(self.registry)

    # -- fake executables ------------------------------------------------
    def _script(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env python3\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return path

    def write_gates(self, inflight_rc=0, contrib_rc=0, rotation_rc=0,
                    contrib_stdout=None, side_effect=None, inflight_stdout=None):
        side = "None" if side_effect is None else repr(side_effect)
        stdout = json.dumps(contrib_stdout if contrib_stdout is not None else gate_json())
        inflight_text = (
            "check-inflight: PASS\n" if inflight_stdout is None
            else inflight_stdout + "\n"
        )
        self.gate_inflight = self._script(
            "check-inflight.py",
            "import sys\nside = %s\nif side:\n    open(side, 'a').write('\\n# mutated\\n')\n"
            "sys.stdout.write(%s)\nsys.exit(%d)\n" % (side, json.dumps(inflight_text), inflight_rc),
        )
        self.gate_contrib = self._script(
            "check-contrib-state.py",
            "import sys\nside = %s\nif side:\n    open(side, 'a').write('\\n# mutated\\n')\n"
            "sys.stdout.write(%s + '\\n')\nsys.exit(%d)\n" % (side, stdout, contrib_rc),
        )
        self.gate_rotation = self._script(
            "check-rotation.py",
            "import sys\nsys.stdout.write('check-rotation: PASS\\n')\nsys.exit(%d)\n"
            % rotation_rc,
        )
        return self.gate_inflight, self.gate_contrib, self.gate_rotation

    def write_fake_hermes(self, broken=False):
        path = os.path.join(self.bin, "hermes")
        if broken:
            # Executable bit set, no valid interpreter line: the spawn raises.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not an executable program\n")
            os.chmod(path, 0o755)
            return path
        body = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "payload = {'argv': sys.argv[1:], 'argv0': sys.argv[0],\n"
            "           'env': dict(os.environ), 'cwd': os.getcwd()}\n"
            "with open(%s, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(payload) + '\\n')\n" % json.dumps(self.child_record)
        )
        return self._script("hermes", body)

    # -- run -------------------------------------------------------------
    def env(self, kill_switch_present=True, extra=None, hermes_bin=None):
        environ = {
            "HOME": self.base,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "PROTEAN_WATCHDOG_ROOT": self.ops,
            "PROTEAN_TASK_HOME": self.task_home,
            "PROTEAN_CONTRIB_STATE": self.contrib,
            "PROTEAN_INFLIGHT": self.inflight,
            "PROTEAN_ROTATION": self.rotation,
            "PROTEAN_HERMES_HOME": self.evalhome,
            "PROTEAN_WATCHDOG_PROJECT": PROJECT,
            "PROTEAN_GATE_INFLIGHT": self.gate_inflight,
            "PROTEAN_GATE_CONTRIB_STATE": self.gate_contrib,
            "PROTEAN_GATE_ROTATION": self.gate_rotation,
            "PROTEAN_SOP_CONTRIB_MODE": self.sop_contrib,
            "PROTEAN_SOP_IDLE_TRIGGER": self.sop_eot,
        }
        if hermes_bin:
            environ["PROTEAN_WATCHDOG_HERMES_BIN"] = hermes_bin
        if extra:
            environ.update(extra)
        return environ

    def run(self, now=NOW_GREEN, extra=None, hermes_bin=None, expect_rc=0):
        completed = subprocess.run(
            WATCHDOG + ["--now", now],
            env=self.env(extra=extra, hermes_bin=hermes_bin),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        if expect_rc is not None and completed.returncode != expect_rc:
            raise AssertionError(
                "exit %d (expected %d); stderr: %s"
                % (completed.returncode, expect_rc, completed.stderr.decode("utf-8", "replace"))
            )
        return completed

    def rows(self):
        if not os.path.exists(self.log_path):
            return []
        text = Path(self.log_path).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.split("\n") if line.strip()]

    def last_row(self):
        rows = self.rows()
        assert rows, "no event row was written"
        return rows[-1]

    def child_calls(self, expect=0, timeout=15.0):
        """Child invocations recorded so far.

        The watchdog deliberately does not wait for the sweep to finish, so
        the fake launcher appends asynchronously: when a test expects a child,
        it must poll for the record rather than race it.
        """
        deadline = time.time() + timeout
        while True:
            if not os.path.exists(self.child_record):
                calls = []
            else:
                text = Path(self.child_record).read_text(encoding="utf-8")
                calls = [json.loads(line) for line in text.split("\n") if line.strip()]
            if len(calls) >= expect or time.time() >= deadline:
                return calls
            time.sleep(0.05)

    def load_state(self):
        return json.loads(Path(self.state_path).read_text(encoding="utf-8"))

    def expected_base(self):
        task = Path(self.task_home).read_bytes()
        inflight_md5 = hashlib.md5(Path(self.inflight).read_bytes()).hexdigest()
        rotation_md5 = hashlib.md5(Path(self.rotation).read_bytes()).hexdigest()
        payload = (
            hashlib.sha256(task).hexdigest().encode("ascii")
            + inflight_md5.encode("ascii")
            + rotation_md5.encode("ascii")
        )
        return hashlib.sha256(payload).hexdigest()

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


def make_row(tick_id, at, dispatch_attempted=True, outcome="skipped",
             reason="all-watchdog-guards-green"):
    row = dict((field, None) for field in EVENT_FIELDS)
    row.update(
        {
            "schema_version": 1,
            "tick_id": tick_id,
            "at": at,
            "outcome": outcome,
            "dispatch_attempted": dispatch_attempted,
            "reasons": [reason],
            "external_contrib": "on",
            "gates": {},
        }
    )
    return row


class WatchdogCase(unittest.TestCase):
    def harness(self, **kwargs):
        made = Harness(**kwargs)
        self.addCleanup(made.cleanup)
        return made

    def assert_skipped(self, harness, reason, expect_rc=0, now=NOW_GREEN):
        harness.run(now=now, expect_rc=expect_rc)
        row = harness.last_row()
        self.assertEqual(row["outcome"], "skipped")
        self.assertEqual(len(harness.child_calls()), 0, "a child was launched on a skip")
        codes = [item.split(":")[0] for item in row["reasons"]]
        self.assertIn(reason, codes, "reasons were %r" % (row["reasons"],))
        self.assertEqual(row["dispatch_attempted"], False)
        return row


class KillSwitchTests(WatchdogCase):
    def test_kill_switch_off_skips_and_launches_nothing(self):
        h = self.harness()
        h.write_contrib(True, "on", "off")
        row = self.assert_skipped(h, "kill-switch-off")
        self.assertEqual(row["external_contrib"], "off")
        self.assertIsNotNone(row["state_md5"], "the first state read was not recorded")
        self.assertLessEqual(list(row["gates"].keys()), ["process_probe"],
                             "a later guard ran after the kill switch")

    def test_missing_kill_switch_state_skips(self):
        h = self.harness()
        h.write_contrib(present=False)
        row = self.assert_skipped(h, "state-absent")
        self.assertEqual(row["external_contrib"], None)

    def test_malformed_kill_switch_state_skips(self):
        for body in ("| external_contrib | maybe |\n", "no switch here at all\n"):
            h = self.harness()
            h.write(h.contrib, body)
            self.assert_skipped(h, "state-unreadable-or-malformed")

    def test_unreadable_kill_switch_state_skips(self):
        h = self.harness()
        h.write_contrib(present=False)
        os.makedirs(os.path.join(h.base, "dir-as-state"), exist_ok=True)
        os.symlink(os.path.join(h.base, "dir-as-state"), h.contrib)
        self.assert_skipped(h, "state-unreadable-or-malformed")


class ProcessTests(WatchdogCase):
    def test_running_worker_skips(self):
        h = self.harness()
        h.write_registry([{"session_id": "proc_aaaaaaaaaaaa", "pid": 4242,
                           "task_id": "demoproject", "pid_scope": "host"}])
        row = self.assert_skipped(h, "worker-running")
        self.assertEqual(row["worker_count"], 1)
        self.assertIn("proc_aaaaaaaaaaaa", " ".join(row["reasons"]))

    def test_process_uncertainty_skips_when_registry_missing(self):
        h = self.harness()
        h.remove_registry()
        row = self.assert_skipped(h, "process-state-unknown")
        self.assertEqual(row["worker_count"], None)
        self.assertEqual(row["gates"]["process_probe"]["exit"], 3)

    def test_process_uncertainty_skips_on_unscoped_registry_entry(self):
        h = self.harness()
        h.write_registry([{"session_id": "proc_bbbbbbbbbbbb", "pid": 4242,
                           "task_id": "", "pid_scope": "host"}])
        self.assert_skipped(h, "process-state-unknown")

    def test_process_uncertainty_skips_on_malformed_registry(self):
        h = self.harness()
        h.write(h.registry, "{not json")
        self.assert_skipped(h, "process-state-unknown")

    def test_process_uncertainty_skips_on_duplicate_handle(self):
        h = self.harness()
        h.write_registry([
            {"session_id": "proc_cccccccccccc", "pid": 1, "task_id": "t", "pid_scope": "host"},
            {"session_id": "proc_cccccccccccc", "pid": 2, "task_id": "t", "pid_scope": "host"},
        ])
        self.assert_skipped(h, "process-state-unknown")

    def test_empty_registry_is_a_valid_zero_worker_proof(self):
        h = self.harness()
        row = None
        h.run()
        row = h.last_row()
        self.assertEqual(row["worker_count"], 0)
        self.assertEqual(row["outcome"], "fired")


class TaskHomeTests(WatchdogCase):
    def test_running_row_skips_without_a_closure_pass(self):
        h = self.harness()
        h.write_task_home(running_rows=1)
        before = Path(h.task_home).read_bytes()
        self.assert_skipped(h, "task-home-running-rows")
        self.assertEqual(Path(h.task_home).read_bytes(), before,
                         "the tick performed a closure pass")

    def test_unblocked_owner_item_skips(self):
        h = self.harness()
        artifact = os.path.join(h.ops, "unblocked-evidence.md")
        h.write(artifact, "the blocking artifact now exists and is non-empty\n")
        h.write_task_home(awaiting_rows=["- item: blocked_on: %s" % artifact])
        row = self.assert_skipped(h, "awaiting-owner-unblocked")
        self.assertEqual(row["awaiting_owner_unblocked"], 1)

    def test_blocked_owner_item_passes(self):
        h = self.harness()
        h.run()
        self.assertEqual(h.last_row()["awaiting_owner_unblocked"], 0)

    def test_missing_blocked_on_skips(self):
        h = self.harness()
        h.write_task_home(awaiting_rows=["- item with no blocker token"])
        self.assert_skipped(h, "awaiting-owner-state-unknown")

    def test_empty_blocked_on_skips(self):
        h = self.harness()
        h.write_task_home(awaiting_rows=["- item: blocked_on:"])
        self.assert_skipped(h, "awaiting-owner-state-unknown")

    def test_unknown_blocker_kind_skips(self):
        h = self.harness()
        h.write_task_home(awaiting_rows=["- item: blocked_on: whatever-happens"])
        self.assert_skipped(h, "awaiting-owner-state-unknown")

    def test_blocker_path_outside_the_configured_roots_skips(self):
        h = self.harness()
        h.write_task_home(
            awaiting_rows=["- item: blocked_on: /etc/hosts"]
        )
        self.assert_skipped(h, "awaiting-owner-state-unknown")

    def test_blocker_traversal_skips(self):
        h = self.harness()
        h.write_task_home(
            awaiting_rows=["- item: blocked_on: %s/../../../etc/hosts" % h.ops]
        )
        self.assert_skipped(h, "awaiting-owner-state-unknown")

    def test_decision_blocker_recorded_in_closed_skips(self):
        h = self.harness()
        h.write_task_home(
            awaiting_rows=["- item: blocked_on: decision:op-42"],
            closed_text="- prior item closed by decision:op-42",
        )
        self.assert_skipped(h, "awaiting-owner-unblocked")

    def test_decision_blocker_not_recorded_passes(self):
        h = self.harness()
        h.write_task_home(awaiting_rows=["- item: blocked_on: decision:op-99"])
        h.run()
        self.assertEqual(h.last_row()["outcome"], "fired")

    def test_malformed_sections_skip(self):
        h = self.harness()
        h.write_task_home_raw("# PROJECT STATE\n\n## RUNNING\n\n## CLOSED\n")
        self.assert_skipped(h, "task-home-unreadable-or-malformed")

    def test_duplicate_section_skips(self):
        h = self.harness()
        h.write_task_home_raw(
            "# P\n\n## RUNNING\n\n## AWAITING OWNER\n\n## RUNNING\n\n## CLOSED\n"
        )
        self.assert_skipped(h, "task-home-unreadable-or-malformed")

    def test_absent_task_home_skips(self):
        h = self.harness()
        os.unlink(h.task_home)
        self.assert_skipped(h, "task-home-absent")


class SharedStateTests(WatchdogCase):
    def test_failing_inflight_gate_skips(self):
        h = self.harness()
        h.write_gates(inflight_rc=1)
        row = self.assert_skipped(h, "gate-failed")
        self.assertEqual(row["gates"]["G-6"]["exit"], 1)

    def test_fail_diagnostic_with_zero_exit_is_treated_as_failure(self):
        """The inflight gate can print FAIL: while its summary exit code is 0."""
        h = self.harness()
        h.write_gates(
            inflight_rc=0,
            inflight_stdout="FAIL: INFLIGHT.md claim 'inf-x': 10 cells, expected 11",
        )
        row = self.assert_skipped(h, "gate-failed")
        self.assertEqual(row["gates"]["G-6"]["exit"], 0)
        self.assertEqual(row["gates"]["G-6"]["fail_lines"], 1)

    def test_short_lease_row_is_rejected_independently_of_the_gate(self):
        h = self.harness()
        h.write_gates()
        h.write_inflight_raw(
            INFLIGHT_HEADER
            + "| inf-a | session | role | provider / model | tree | files "
              "| claim time | lease expiry | none | active |\n"
        )
        row = self.assert_skipped(h, "inflight-malformed-row")
        self.assertEqual(row["gates"]["G-6"]["exit"], 0)

    def test_internal_off_is_illegal(self):
        h = self.harness()
        h.write_gates(contrib_stdout=gate_json(internal="off"))
        self.assert_skipped(h, "contrib-state-illegal")

    def test_external_not_on_is_illegal(self):
        h = self.harness()
        h.write_gates(contrib_stdout=gate_json(external="off"))
        self.assert_skipped(h, "contrib-state-illegal")

    def test_unparseable_contrib_gate_output_skips(self):
        h = self.harness()
        h.write_gates(contrib_stdout="not json at all")
        self.assert_skipped(h, "contrib-state-illegal")

    def test_rotation_gate_failure_skips(self):
        h = self.harness()
        h.write_gates(rotation_rc=2)
        self.assert_skipped(h, "gate-failed")

    def test_state_change_during_admission_skips(self):
        h = self.harness()
        h.write_gates(side_effect=h.contrib)
        row = self.assert_skipped(h, "state-changed-during-admission")
        self.assertEqual(len(h.child_calls()), 0)

    def test_missing_inflight_section_skips(self):
        h = self.harness()
        h.write_inflight_raw("# INFLIGHT\n\n## Columns (fixed)\n\n| a | b |\n")
        self.assert_skipped(h, "inflight-section-missing")


class DebounceTests(WatchdogCase):
    def test_lock_collision_skips_without_waiting_or_deleting(self):
        import fcntl

        h = self.harness()
        lock_path = os.path.join(h.ops, "var", "idle-watchdog.lock")
        holder = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            row = self.assert_skipped(h, "singleton-lock-held")
            self.assertTrue(os.path.exists(lock_path),
                            "the lock file was removed by a collision")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        self.assertEqual(row["pid"], None)

    def test_cooldown_active_skips(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["last_dispatch_at"] = TIMES["cooldown_start"]
        state["last_attempt_status"] = "fired"
        h.write_state(state)
        row = self.assert_skipped(h, "cooldown-active")
        self.assertEqual(row["cooldown_until"], TIMES["cooldown_until"])

    def test_cooldown_expired_fires(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["last_dispatch_at"] = TIMES["cooldown_expired"]
        state["last_attempt_status"] = "fired"
        h.write_state(state)
        h.run()
        self.assertEqual(h.last_row()["outcome"], "fired")
        self.assertEqual(len(h.child_calls(1)), 1)

    def test_same_base_fingerprint_skips(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["last_fired_base_fingerprint"] = h.expected_base()
        h.write_state(state)
        row = self.assert_skipped(h, "same-idle-state-already-fired")
        self.assertEqual(row["base_fingerprint"], h.expected_base())

    def test_changed_canonical_state_gets_a_new_base_fingerprint(self):
        h = self.harness()
        first = h.expected_base()
        h.write_task_home(awaiting_rows=["- item: blocked_on: decision:op-7"])
        self.assertNotEqual(first, h.expected_base())
        h.run()
        self.assertEqual(h.last_row()["outcome"], "fired")

    def test_daily_cap_skips_at_four_attempts(self):
        h = self.harness()
        rows = [
            make_row(_tick_id(_shift(NOW_GREEN, minutes=-20 - index)),
                     _shift(NOW_GREEN, minutes=-20 - index))
            for index in range(4)
        ]
        h.write(h.log_path, "".join(json.dumps(row) + "\n" for row in rows))
        row = self.assert_skipped(h, "daily-watchdog-cap")
        self.assertEqual(row["daily_attempts_utc"], 4)

    def test_previous_day_attempts_do_not_count(self):
        h = self.harness()
        rows = [
            make_row(_tick_id(_shift(NOW_GREEN, hours=-30, minutes=-index)),
                     _shift(NOW_GREEN, hours=-30, minutes=-index))
            for index in range(4)
        ]
        h.write(h.log_path, "".join(json.dumps(row) + "\n" for row in rows))
        h.run()
        self.assertEqual(h.last_row()["outcome"], "fired")
        self.assertEqual(h.last_row()["daily_attempts_utc"], 0)

    def test_invalid_historical_row_skips_rather_than_being_ignored(self):
        h = self.harness()
        h.write(h.log_path, '{"schema_version": 1, "outcome": "fired"}\n')
        self.assert_skipped(h, "event-log-invalid")

    def test_absent_event_log_skips(self):
        h = self.harness()
        os.unlink(h.log_path)
        self.assert_skipped(h, "event-log-absent")

    def test_pending_reservation_blocks_and_is_never_auto_cleared(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["pending_attempt"] = {
            "tick_id": "tick-20260917-140000-deadbeef",
            "base_fingerprint": "b" * 64,
            "reserved_at": "2026-09-17T13:00:00Z",
            "status": "reserved",
        }
        h.write_state(state)
        # Age the file far beyond any plausible lease: age must not matter.
        os.utime(h.state_path, (0, 0))
        self.assert_skipped(h, "pending-reservation")
        self.assertEqual(h.load_state(), state, "the reservation was silently changed")
        self.assertEqual(os.stat(h.state_path).st_mtime, 0,
                         "the reservation file was rewritten")

    def test_malformed_watchdog_state_skips(self):
        for state in ({"schema_version": 2}, {"schema_version": 1, "unexpected": 1}, []):
            h = self.harness()
            h.write(h.state_path, json.dumps(state))
            self.assert_skipped(h, "watchdog-state-unreadable-or-malformed")

    def test_absent_watchdog_state_skips(self):
        h = self.harness()
        os.unlink(h.state_path)
        self.assert_skipped(h, "watchdog-state-absent")

    def test_missing_timestamp_skips(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["last_dispatch_at"] = "yesterday"
        h.write_state(state)
        self.assert_skipped(h, "watchdog-state-unreadable-or-malformed")


class QuietHoursTests(WatchdogCase):
    def test_outside_the_window_skips(self):
        h = self.harness()
        row = self.assert_skipped(h, "quiet-hours", now=NOW_QUIET)
        self.assertEqual(len(h.child_calls()), 0)
        self.assertIsNotNone(row["base_fingerprint"])

    def test_inside_the_window_fires(self):
        h = self.harness()
        h.run(now=NOW_GREEN)
        self.assertEqual(h.last_row()["outcome"], "fired")

    def test_unavailable_timezone_skips(self):
        h = self.harness()
        bogus = os.path.join(h.base, "no-tz")
        os.makedirs(bogus, exist_ok=True)
        completed = subprocess.run(
            WATCHDOG + ["--now", NOW_GREEN],
            env=h.env(extra={"PYTHONTZPATH": bogus}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        codes = [item.split(":")[0] for item in h.last_row()["reasons"]]
        self.assertIn("timezone-unavailable", codes)
        self.assertEqual(len(h.child_calls()), 0)


class GreenTickTests(WatchdogCase):
    def test_green_tick_fires_exactly_one_profile_native_child(self):
        h = self.harness()
        h.run()
        row = h.last_row()
        self.assertEqual(row["outcome"], "fired")
        self.assertEqual(row["dispatch_attempted"], True)
        self.assertEqual(row["reasons"][-1], "all-watchdog-guards-green")
        self.assertIn("all-watchdog-guards-green", row["reasons"])
        self.assertEqual(row["worker_count"], 0)
        self.assertEqual(row["awaiting_owner_unblocked"], 0)
        self.assertEqual(row["daily_attempts_utc"], 0)
        self.assertEqual(row["cooldown_until"], _shift(NOW_GREEN, hours=2))
        self.assertIsInstance(row["pid"], int)
        self.assertEqual(row["base_fingerprint"], h.expected_base())

        calls = h.child_calls(1)
        self.assertEqual(len(calls), 1, "the tick did not launch exactly one child")
        call = calls[0]
        self.assertEqual(
            call["argv"],
            ["chat", "--oneshot", "--query-file",
             os.path.join(h.ops, "briefs", "idle-watchdog-sweep.md")],
        )
        self.assertEqual(call["env"]["HERMES_HOME"], h.evalhome)
        self.assertEqual(call["env"]["PROTEAN_WATCHDOG_TICK_ID"], row["tick_id"])
        self.assertEqual(call["env"]["PROTEAN_WATCHDOG_BASE_FINGERPRINT"],
                         row["base_fingerprint"])
        self.assertTrue(
            call["env"]["PROTEAN_WATCHDOG_RECEIPT"].endswith("%s.md" % row["tick_id"])
        )
        self.assertEqual(call["env"]["PROTEAN_WATCHDOG_ROOT"], h.ops)
        self.assertEqual(call["env"]["PROTEAN_TASK_HOME"], h.task_home)
        self.assertEqual(call["env"]["PROTEAN_CONTRIB_STATE"], h.contrib)

    def test_fired_tick_records_state_and_sweep_log(self):
        h = self.harness()
        h.run()
        row = h.last_row()
        state = h.load_state()
        self.assertEqual(state["last_attempt_status"], "fired")
        self.assertEqual(state["last_attempt_tick_id"], row["tick_id"])
        self.assertEqual(state["last_fired_base_fingerprint"], row["base_fingerprint"])
        self.assertIsNone(state["pending_attempt"])
        self.assertEqual(state["last_dispatch_at"], NOW_GREEN)
        self.assertTrue(
            os.path.exists(os.path.join(h.ops, "logs", "sweeps", "%s.log" % row["tick_id"]))
        )
        diagnostic = os.path.join(h.ops, "logs", "dispatch", "%s.json" % row["tick_id"])
        self.assertTrue(os.path.exists(diagnostic))
        payload = json.loads(Path(diagnostic).read_text(encoding="utf-8"))
        self.assertEqual(payload["provider"], "fixture-provider")
        self.assertEqual(payload["model"], "fixture-model")

    def test_absolute_paths_survive_spaces(self):
        h = self.harness(space=True)
        self.assertIn(" ", h.ops)
        h.run()
        call = h.child_calls(1)[0]
        self.assertEqual(call["env"]["PROTEAN_WATCHDOG_ROOT"], h.ops)
        self.assertEqual(call["argv"][-1],
                         os.path.join(h.ops, "briefs", "idle-watchdog-sweep.md"))

    def test_tick_does_not_depend_on_the_current_directory(self):
        h = self.harness()
        completed = subprocess.run(
            WATCHDOG + ["--now", NOW_GREEN],
            env=h.env(),
            cwd="/",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(h.child_calls(1)), 1)

    def test_missing_brief_skips(self):
        h = self.harness()
        os.unlink(os.path.join(h.ops, "briefs", "idle-watchdog-sweep.md"))
        self.assert_skipped(h, "brief-missing")

    def test_missing_locked_procedure_path_skips(self):
        h = self.harness()
        row = None
        completed = subprocess.run(
            WATCHDOG + ["--now", NOW_GREEN],
            env=h.env(extra={"PROTEAN_SOP_IDLE_TRIGGER": ""}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        row = h.last_row()
        codes = [item.split(":")[0] for item in row["reasons"]]
        self.assertIn("config-missing", codes)
        self.assertEqual(len(h.child_calls()), 0)

    def test_unreadable_profile_config_skips(self):
        h = self.harness()
        h.write(h.evalhome + "/config.yaml", "provider: fixture-provider\n")
        self.assert_skipped(h, "profile-config-unreadable")

    def test_missing_hermes_executable_skips(self):
        h = self.harness()
        os.unlink(os.path.join(h.bin, "hermes"))
        home = os.path.join(h.base, "empty-bin")
        os.makedirs(home, exist_ok=True)
        completed = subprocess.run(
            WATCHDOG + ["--now", NOW_GREEN],
            env=h.env(extra={"PATH": home + os.pathsep + "/usr/bin:/bin"}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        codes = [item.split(":")[0] for item in h.last_row()["reasons"]]
        self.assertIn("hermes-executable-not-found", codes)
        self.assertEqual(len(h.child_calls()), 0)


class LauncherFailureTests(WatchdogCase):
    def test_launcher_error_consumes_an_attempt_and_stays_fail_closed(self):
        h = self.harness()
        h.write_fake_hermes(broken=True)
        h.run()
        row = h.last_row()
        self.assertEqual(row["outcome"], "skipped")
        self.assertEqual(row["dispatch_attempted"], True)
        self.assertIn("launcher-error", row["reasons"])
        state = h.load_state()
        self.assertEqual(state["last_attempt_status"], "launcher-error")
        self.assertIsNone(state["pending_attempt"])
        self.assertIsNotNone(state["last_dispatch_at"])
        self.assertEqual(len(h.child_calls()), 0)

        # The very next tick must not retry: the cooldown holds it.
        h.run()
        self.assertEqual(h.last_row()["outcome"], "skipped")
        codes = [item.split(":")[0] for item in h.last_row()["reasons"]]
        self.assertIn("cooldown-active", codes)
        self.assertEqual(len(h.rows()), 2)

    def test_launcher_error_attempt_counts_toward_the_daily_cap(self):
        h = self.harness()
        h.write_fake_hermes(broken=True)
        # Three-hour spacing clears the two-hour cooldown, so every tick attempts.
        slots = TIMES["green_slots"]
        picks = [slots[i] for i in (0, len(slots) // 4, len(slots) // 2,
                                    (3 * len(slots)) // 4)]
        for when in picks:
            h.run(now=when)
            self.assertEqual(h.last_row()["dispatch_attempted"], True,
                             "attempt at %s did not consume a slot" % when)
        self.assertEqual(h.last_row()["daily_attempts_utc"], 3)
        # A fifth tick (cooldown expired) sees four attempts consumed and stops.
        h.run(now=_shift(TIMES["green_slots"][-1], minutes=5))
        row = h.last_row()
        self.assertEqual(row["dispatch_attempted"], False)
        self.assertEqual(row["daily_attempts_utc"], 4)
        codes = [item.split(":")[0] for item in row["reasons"]]
        self.assertIn("daily-watchdog-cap", codes)

    def test_recovery_refuses_without_the_operator_assertion(self):
        h = self.harness()
        state = dict(SEED_STATE)
        state["pending_attempt"] = {
            "tick_id": "tick-20260917-140000-deadbeef",
            "base_fingerprint": "b" * 64,
            "reserved_at": "2026-09-17T13:00:00Z",
            "status": "reserved",
        }
        h.write_state(state)
        completed = subprocess.run(
            WATCHDOG + ["--clear-pending-reservation",
                        "--tick-id", "tick-20260917-140000-deadbeef"],
            env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(h.load_state(), state)
        self.assertEqual(len(h.child_calls()), 0)

    def test_recovery_clears_only_the_exact_reservation(self):
        h = self.harness()
        pending = {
            "tick_id": "tick-20260917-140000-deadbeef",
            "base_fingerprint": "b" * 64,
            "reserved_at": "2026-09-17T13:00:00Z",
            "status": "reserved",
        }
        state = dict(SEED_STATE)
        state["pending_attempt"] = pending
        h.write_state(state)
        completed = subprocess.run(
            WATCHDOG + ["--clear-pending-reservation",
                        "--tick-id", "tick-20260917-140000-deadbeef",
                        "--confirm-no-child-alive"],
            env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        after = h.load_state()
        self.assertIsNone(after["pending_attempt"])
        self.assertEqual(after["last_attempt_status"], "launcher-error")


class StormTests(WatchdogCase):
    def test_repeated_ticks_never_storm(self):
        h = self.harness()
        h.run(now=NOW_GREEN)
        self.assertEqual(len(h.child_calls(1)), 1)
        for minute in (15, 30, 45):
            h.run(now=_shift(NOW_GREEN, minutes=minute))
        self.assertEqual(len(h.child_calls(1)), 1, "a second child was launched")
        rows = h.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["outcome"], "fired")
        for row in rows[1:]:
            self.assertEqual(row["outcome"], "skipped")
            codes = [item.split(":")[0] for item in row["reasons"]]
            self.assertTrue(
                {"cooldown-active", "same-idle-state-already-fired"} & set(codes),
                "later tick was not debounced: %r" % (row["reasons"],),
            )

    def test_same_state_after_cooldown_is_blocked_by_the_one_fire_marker(self):
        h = self.harness()
        h.run(now=NOW_GREEN)
        state = h.load_state()
        state["last_dispatch_at"] = TIMES["cooldown_expired"]
        h.write_state(state)
        h.run(now=NOW_GREEN)
        self.assertEqual(len(h.child_calls(1)), 1)
        codes = [item.split(":")[0] for item in h.last_row()["reasons"]]
        self.assertIn("same-idle-state-already-fired", codes)

    def test_every_attempt_row_is_well_formed(self):
        h = self.harness()
        h.write_contrib(True, "on", "off")
        h.run()
        h.write_contrib(present=False)
        h.run()
        rows = h.rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(list(row.keys()), EVENT_FIELDS)
            self.assertEqual(row["schema_version"], 1)
            self.assertIn(row["outcome"], ("fired", "skipped"))
            self.assertTrue(row["reasons"])
            self.assertTrue(all(isinstance(item, str) and item for item in row["reasons"]))


class SecretTests(WatchdogCase):
    CANARY = "sk-canary0000000000000000"

    def test_no_secret_reaches_the_child_logs_or_state(self):
        h = self.harness()
        extra = {
            "FIXTURE_API_KEY": self.CANARY,
            "SOME_TOKEN": self.CANARY,
            "ANTHROPIC_API_KEY": self.CANARY,
            "AWS_SECRET_ACCESS_KEY": self.CANARY,
            "GITHUB_TOKEN": self.CANARY,
        }
        h.run(extra=extra)
        calls = h.child_calls(1)
        self.assertEqual(len(calls), 1)
        child_env = calls[0]["env"]
        for name, value in child_env.items():
            self.assertNotEqual(value, self.CANARY, "secret value reached %s" % name)
            self.assertNotIn(self.CANARY, value)
        for name in child_env:
            self.assertNotIn("TOKEN", name.upper())
            self.assertNotIn("SECRET", name.upper())
            self.assertNotIn("API_KEY", name.upper())
        payloads = [
            Path(h.log_path).read_text(encoding="utf-8"),
            h.state_path and Path(h.state_path).read_text(encoding="utf-8") or "",
        ]
        for path in (os.path.join(h.ops, "logs", "dispatch", "%s.json"
                                  % h.last_row()["tick_id"]),):
            payloads.append(Path(path).read_text(encoding="utf-8"))
        for payload in payloads:
            self.assertNotIn(self.CANARY, payload)

    def test_child_environment_is_a_fixed_allowlist(self):
        h = self.harness()
        h.run(extra={"OPERATOR_PRIVATE_VAR": "value-that-must-not-propagate"})
        child_env = h.child_calls(1)[0]["env"]
        self.assertNotIn("OPERATOR_PRIVATE_VAR", child_env)
        self.assertNotIn("FIXTURE_API_KEY", child_env)
        # The watchdog contributes exactly the allowlist. The OS may add its
        # own defaults when exec'ing the fake's shebang interpreter, so the
        # assertion is on the watchdog-owned namespace, not on the full dict.
        watchdog_owned = [name for name in child_env
                          if name.startswith("PROTEAN_") or name in
                          ("HOME", "PATH", "HERMES_HOME")]
        self.assertEqual(
            sorted(watchdog_owned),
            sorted([
                "HOME", "PATH", "HERMES_HOME", "PROTEAN_WATCHDOG_ROOT",
                "PROTEAN_TASK_HOME", "PROTEAN_CONTRIB_STATE",
                "PROTEAN_WATCHDOG_TICK_ID", "PROTEAN_WATCHDOG_BASE_FINGERPRINT",
                "PROTEAN_WATCHDOG_RECEIPT", "PROTEAN_SOP_CONTRIB_MODE",
                "PROTEAN_SOP_IDLE_TRIGGER",
            ]),
        )
        self.assertNotIn("PROTEAN_WATCHDOG_PROJECT", child_env,
                         "non-allowlisted PROTEAN_* value propagated")

    def test_logger_rejects_a_secret_shaped_row(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import importlib.util

        spec = importlib.util.spec_from_file_location("idle_watchdog", str(SCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        row = make_row("tick-20260917-140000-aaaaaaaa", "2026-09-17T14:00:00Z")
        row["reasons"] = [self.CANARY]
        with self.assertRaises(module.LogSchemaError):
            module.validate_row(row)
        row = make_row("tick-20260917-140000-aaaaaaaa", "2026-09-17T14:00:00Z")
        row["reasons"] = []
        with self.assertRaises(module.LogSchemaError):
            module.validate_row(row)
        row = make_row("tick-20260917-140000-aaaaaaaa", "2026-09-17T14:00:00Z")
        row["extra_field"] = "x"
        with self.assertRaises(module.LogSchemaError):
            module.validate_row(row)


class ProbeContractTests(WatchdogCase):
    def probe(self, h, extra=None):
        return subprocess.run(
            WATCHDOG + ["--process-probe-json", "--now", NOW_GREEN],
            env=h.env(extra=extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )

    def test_probe_reports_an_authoritative_empty_set(self):
        h = self.harness()
        completed = self.probe(h)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertEqual(sorted(envelope.keys()),
                         ["authoritative", "generated_at", "project", "schema_version",
                          "workers"])
        self.assertIs(envelope["authoritative"], True)
        self.assertEqual(envelope["workers"], [])
        self.assertEqual(envelope["project"], PROJECT)

    def test_probe_reports_one_running_worker(self):
        h = self.harness()
        h.write_registry([{"session_id": "proc_1234567890ab", "pid": 99,
                           "task_id": "demoproject", "pid_scope": "host"}])
        completed = self.probe(h)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertEqual(envelope["workers"],
                         [{"handle": "proc_1234567890ab", "state": "running"}])

    def test_probe_fails_closed_without_a_registry(self):
        h = self.harness()
        h.remove_registry()
        completed = self.probe(h)
        self.assertEqual(completed.returncode, 3)
        envelope = json.loads(completed.stdout)
        self.assertIs(envelope["authoritative"], False)
        self.assertEqual(envelope["workers"], [])

    def test_guard_consumes_the_adapter_result_not_a_notification(self):
        """A live registry entry stops the tick; no remembered state is trusted."""
        h = self.harness()
        h.write_registry([{"session_id": "proc_1234567890ab", "pid": 99,
                           "task_id": "demoproject", "pid_scope": "host"}])
        row = self.assert_skipped(h, "worker-running")
        self.assertEqual(row["worker_count"], 1)


class ArgumentTests(WatchdogCase):
    def test_missing_root_exits_two(self):
        completed = subprocess.run(
            WATCHDOG, env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(completed.returncode, 2)

    def test_bad_clock_override_exits_two(self):
        h = self.harness()
        completed = subprocess.run(
            WATCHDOG + ["--now", "not-a-time"], env=h.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(completed.returncode, 2)

    def test_relative_root_exits_two(self):
        h = self.harness()
        completed = subprocess.run(
            WATCHDOG + ["--now", NOW_GREEN],
            env=h.env(extra={"PROTEAN_WATCHDOG_ROOT": "relative/path"}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(completed.returncode, 2)


class CronSpecTests(WatchdogCase):
    """The shipped cron line must agree with the environment the script reads.

    A cron specification that names a variable the script never reads installs
    a tick that stops before any guard: the operator gets a silent misinstall.
    Both directions are asserted here.
    """

    def cron_variables(self):
        text = (REPO / "cron" / "idle-watchdog.cron").read_text()
        line = [row for row in text.splitlines()
                if row.startswith("*/15 * * * *")]
        self.assertEqual(len(line), 1, "the specification must carry exactly one schedule line")
        return line[0]

    def test_every_required_variable_appears_in_the_cron_line(self):
        line = self.cron_variables()
        required = [
            "PROTEAN_WATCHDOG_ROOT", "PROTEAN_TASK_HOME", "PROTEAN_CONTRIB_STATE",
            "PROTEAN_INFLIGHT", "PROTEAN_ROTATION", "PROTEAN_HERMES_HOME",
            "PROTEAN_GATE_INFLIGHT", "PROTEAN_GATE_CONTRIB_STATE",
            "PROTEAN_GATE_ROTATION", "PROTEAN_SOP_CONTRIB_MODE",
            "PROTEAN_SOP_IDLE_TRIGGER",
        ]
        for name in required:
            self.assertIn(name + "=", line, "%s is missing from the cron line" % name)

    def test_no_variable_the_script_never_reads_is_exported(self):
        line = self.cron_variables()
        assigned = set(re.findall(r"([A-Z_][A-Z0-9_]*)=", line))
        script = (REPO / "scripts" / "idle-watchdog.py").read_text()
        for name in sorted(assigned):
            if name == "HOME":
                continue  # cron gives almost no environment; HOME is passed on purpose
            self.assertIn('"%s"' % name, script,
                          "%s is set by cron but never read by the watchdog" % name)

    def test_the_schedule_line_is_quoted_for_spaces_and_execs_once(self):
        line = self.cron_variables()
        self.assertIn('"__OPS_ROOT__/scripts/idle-watchdog.py"', line)
        self.assertEqual(line.count("exec /usr/bin/python3"), 1)
        self.assertIn('>> "__OPS_ROOT__/logs/cron.log" 2>&1', line)
        self.assertNotIn("@every", line)
        self.assertNotIn("&&", line)

    def test_every_placeholder_is_documented_in_the_substitution_table(self):
        text = (REPO / "cron" / "idle-watchdog.cron").read_text()
        line = self.cron_variables()
        used = set(re.findall(r"__(?:[A-Z_]+)__", line))
        documented = set(re.findall(r"^#\s+(__[A-Z_]+__)", text, flags=re.M))
        self.assertEqual(used - documented, set(),
                         "undocumented placeholder in the schedule line")
        self.assertEqual(documented - used, set(),
                         "documented placeholder the schedule line never uses")


    def test_the_schedule_line_parses_as_shell_with_spaces_in_paths(self):
        line = self.cron_variables()
        substituted = line
        for placeholder, value in (
            ("__HOME__", "/fixture-root"),
            ("__EVALUATOR_HOME__", "/fixture-root/evaluator home"),
            ("__OPS_ROOT__", "/fixture-root/ops root"),
            ("__TASK_HOME__", "/fixture-root/records/TASK-HOME.md"),
            ("__CONTRIB_STATE__", "/fixture-root/records/CONTRIB-STATE.md"),
            ("__INFLIGHT__", "/fixture-root/records/INFLIGHT.md"),
            ("__ROTATION__", "/fixture-root/records/ROTATION-STATE.md"),
            ("__GATE_SCRIPTS__", "/fixture-root/ops root/scripts/protean-ops"),
            ("__SOP_CONTRIB_MODE__", "/fixture-root/sops/contrib mode.md"),
            ("__SOP_IDLE_TRIGGER__", "/fixture-root/sops/idle trigger.md"),
            ("__PROJECT__", "example-project"),
        ):
            substituted = substituted.replace(placeholder, value)
        self.assertNotIn("__", substituted)
        command = substituted.split("; ", 1)[1] if "; " in substituted else substituted
        completed = subprocess.run(["/bin/sh", "-n"], input=command.encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout.decode())
        self.assertIn('exec /usr/bin/python3 "/fixture-root/ops root/scripts/idle-watchdog.py"',
                      command)
        self.assertIn('>> "/fixture-root/ops root/logs/cron.log"', command)


if __name__ == "__main__":
    unittest.main()
