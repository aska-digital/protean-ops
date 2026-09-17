#!/usr/bin/env python3
"""Focused fixtures for the contribution-state gate (G-14).

Each rule gets at least one GREEN case and one RED case; RED cases are
single-cause so a failure names exactly the rule under test. Run:

  python3 -m unittest tests.test_contrib_state
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "protean-ops", "check-contrib-state.py")
VALID = os.path.join(ROOT, "tests", "fixtures", "valid", "CONTRIB-STATE.md")
TEMPLATE = os.path.join(ROOT, "records", "CONTRIB-STATE.md.tmpl")
INVALID = os.path.join(ROOT, "tests", "fixtures", "invalid")

HEADER = ("# CONTRIB-STATE - fixture\n\n## Header\n\n| key | value |\n|---|---|\n"
          "| schema_version | 1 |\n| internal_contrib | %s |\n| external_contrib | %s |\n"
          "| read_at | 2026-09-17T15:00:00Z |\n| mode_epoch | - |\n"
          "| last_change | 2026-09-17T15:00:00Z |\n\n## Rows\n\n"
          "| row id | kind | repo | action | thread | mode | authority | detail "
          "| lane / session | time | evidence |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|\n")

WITHIN_QUIET_HOURS = "2026-09-17T15:00:00Z"      # 11:00 America/New_York
OUTSIDE_QUIET_HOURS = "2026-09-18T02:30:00Z"     # 22:30 America/New_York


def run_gate(*args):
    result = subprocess.run([sys.executable, GATE] + [str(a) for a in args],
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout + result.stderr


class StateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, name="CONTRIB-STATE.md"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def state(self, external="off", rows="", internal="on"):
        if external == "on":
            rows = ("| cc-sw | switch | - | - | - | external | approval op-1 | "
                    "external_contrib=on operator reference op-1 | installer | "
                    "2026-09-17T09:00:00Z | evidence/x |\n") + rows
        return self.write(HEADER % (internal, external) + rows)


class TestShippedFixtures(StateCase):
    def test_help_exits_zero(self):
        code, out = run_gate("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("Usage", out)

    def test_valid_fixture_passes(self):
        code, out = run_gate(VALID)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_empty_template_passes(self):
        code, out = run_gate(TEMPLATE)
        self.assertEqual(code, 0, out)

    def test_invalid_fixtures_fail(self):
        for name in sorted(os.listdir(INVALID)):
            if not name.endswith(".md"):
                continue
            code, out = run_gate(os.path.join(INVALID, name))
            self.assertEqual(code, 1, "%s did not fail: %s" % (name, out))

    def test_self_test_suite_passes(self):
        code, out = run_gate("--self-test")
        self.assertEqual(code, 0, out)
        self.assertIn("failed=0", out)


class TestToggle(StateCase):
    def test_absent_record_reads_external_off_and_internal_on(self):
        missing = os.path.join(self._tmp.name, "absent.md")
        code, out = run_gate(missing)
        self.assertEqual(code, 0, out)
        self.assertIn("value=off", out)
        self.assertIn("internal_mode = on", out)

    def test_absent_record_refuses_an_external_write(self):
        missing = os.path.join(self._tmp.name, "absent.md")
        code, out = run_gate(missing, "--target", "external", "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 1, out)

    def test_illegal_enum_fails_closed(self):
        code, out = run_gate(self.state(external="maybe"))
        self.assertEqual(code, 1, out)
        self.assertIn("external_contrib", out)

    def test_internal_off_is_refused_without_a_decision_id(self):
        code, out = run_gate(self.state(internal="off"))
        self.assertEqual(code, 1, out)
        self.assertIn("internal_contrib", out)

    def test_internal_off_is_accepted_with_a_cited_decision(self):
        rows = ("| cc-1 | note | - | - | - | internal | - | decision: D-9 operator decision "
                "| lane | 2026-09-17T10:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(internal="off", rows=rows))
        self.assertEqual(code, 0, out)

    def test_internal_write_is_allowed_while_external_is_off(self):
        code, out = run_gate(self.state(external="off"), "--target", "internal",
                             "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 0, out)

    def test_external_on_without_a_switch_row_is_refused(self):
        raw = HEADER % ("on", "on")
        code, out = run_gate(self.write(raw))
        self.assertEqual(code, 1, out)
        self.assertIn("switch", out)


class TestWriteLimits(StateCase):
    def test_external_write_inside_quiet_hours_passes(self):
        code, out = run_gate(self.state(external="on"), "--now", WITHIN_QUIET_HOURS,
                             "--target", "external", "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 0, out)

    def test_external_write_outside_quiet_hours_is_refused(self):
        code, out = run_gate(self.state(external="on"), "--now", OUTSIDE_QUIET_HOURS,
                             "--target", "external", "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 1, out)
        self.assertIn("quiet hours", out)

    def test_quiet_hours_boundary_is_inclusive_at_the_start(self):
        code, out = run_gate(self.state(external="on"), "--now", "2026-09-17T09:00:00Z",
                             "--target", "external", "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 0, out)

    def test_a_second_external_pr_inside_24h_is_refused(self):
        rows = ("| cc-1 | submitted | o/r | pr | 12 | external | approval op-1 | - | lane "
                "| 2026-09-17T10:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows), "--now", WITHIN_QUIET_HOURS,
                             "--target", "external", "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 1, out)
        self.assertIn("rate", out)

    def test_three_live_lanes_are_refused(self):
        rows = "".join(
            "| cc-l%d | lane | o/r | pr | - | external | approval op-1 | "
            "lease_until=2026-09-17T20:00:00Z | lane | 2026-09-17T10:00:00Z | evidence/x |\n" % i
            for i in (1, 2, 3))
        code, out = run_gate(self.state(external="on", rows=rows), "--now", WITHIN_QUIET_HOURS)
        self.assertEqual(code, 1, out)
        self.assertIn("cap is 2", out)

    def test_an_expired_lease_is_not_a_live_lane(self):
        rows = ("| cc-l1 | lane | o/r | pr | - | external | approval op-1 | "
                "lease_until=2026-09-17T10:00:00Z | lane | 2026-09-17T08:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows), "--now", WITHIN_QUIET_HOURS)
        self.assertEqual(code, 0, out)

    def test_the_third_write_to_one_repo_inside_an_hour_is_refused(self):
        rows = "".join(
            "| cc-b%d | submitted | o/r | comment | %d | external | approval op-1 | - | lane "
            "| 2026-09-17T14:%02d:00Z | evidence/x |\n" % (i, i, 40 + i) for i in (1, 2))
        code, out = run_gate(self.state(external="on", rows=rows), "--now", WITHIN_QUIET_HOURS,
                             "--target", "external", "--repo", "o/r", "--action", "comment")
        self.assertEqual(code, 1, out)
        self.assertIn("burst", out)

    def test_an_external_merge_is_never_autonomous(self):
        code, out = run_gate(self.state(external="on"), "--target", "external",
                             "--repo", "o/r", "--action", "merge")
        self.assertEqual(code, 1, out)
        self.assertIn("merge", out)

    def test_a_write_query_without_a_target_is_refused(self):
        code, out = run_gate(self.state(external="on"), "--repo", "o/r", "--action", "pr")
        self.assertEqual(code, 2, out)


class TestGrants(StateCase):
    def test_a_valid_grant_passes(self):
        rows = ("| cc-1 | grant | o/r | comment | 7 | external | grant g1 | grant_id=g1 "
                "class=comment scope=o/r expires=2026-09-24T00:00:00Z max_actions=1 "
                "gates=qa-pass | - | 2026-09-17T09:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows), "--now", WITHIN_QUIET_HOURS)
        self.assertEqual(code, 0, out)

    def test_an_expired_grant_is_refused(self):
        rows = ("| cc-1 | grant | o/r | comment | 7 | external | grant g1 | grant_id=g1 "
                "class=comment scope=o/r expires=2026-09-01T00:00:00Z max_actions=1 | - "
                "| 2026-09-01T09:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows))
        self.assertEqual(code, 1, out)
        self.assertIn("expired", out)

    def test_a_grant_missing_a_requirement_is_refused(self):
        rows = ("| cc-1 | grant | o/r | comment | 7 | external | grant g1 | grant_id=g1 "
                "class=comment scope=o/r max_actions=1 | - | 2026-09-17T09:00:00Z | "
                "evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows))
        self.assertEqual(code, 1, out)
        self.assertIn("expires", out)

    def test_consumption_past_max_actions_is_refused(self):
        rows = ("| cc-1 | grant | o/r | comment | 7 | external | grant g1 | grant_id=g1 "
                "class=comment scope=o/r expires=2026-09-24T00:00:00Z max_actions=1 | - "
                "| 2026-09-17T09:00:00Z | evidence/x |\n"
                "| cc-2 | consumed | o/r | comment | 7 | external | grant g1 | grant_id=g1 "
                "used | lane | 2026-09-17T10:00:00Z | evidence/x |\n"
                "| cc-3 | consumed | o/r | comment | 8 | external | grant g1 | grant_id=g1 "
                "used | lane | 2026-09-17T11:00:00Z | evidence/x |\n")
        code, out = run_gate(self.state(external="on", rows=rows))
        self.assertEqual(code, 1, out)
        self.assertIn("consumption rows", out)


class TestRowShape(StateCase):
    def test_a_row_with_the_wrong_cell_count_is_refused(self):
        rows = "| cc-1 | note | - | - |\n"
        code, out = run_gate(self.state(rows=rows))
        self.assertEqual(code, 1, out)
        self.assertIn("cells", out)

    def test_an_unparseable_timestamp_is_refused(self):
        rows = ("| cc-1 | submitted | o/r | pr | 1 | internal | internal-default | - | lane "
                "| yesterday | evidence/x |\n")
        code, out = run_gate(self.state(rows=rows))
        self.assertEqual(code, 1, out)
        self.assertIn("ISO-8601", out)

    def test_a_missing_header_key_is_refused(self):
        code, out = run_gate(self.write("# CONTRIB-STATE\n\n## Header\n\n| key | value |\n"
                                        "|---|---|\n| external_contrib | off |\n"))
        self.assertEqual(code, 1, out)

    def test_json_output_is_parseable(self):
        code, out = run_gate(self.state(), "--json")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["gate"], "G-14")
        self.assertEqual(payload["external_contrib"], "off")


class TestDescriptor(unittest.TestCase):
    def test_the_descriptor_declares_the_gate_and_a_matching_version(self):
        with open(os.path.join(ROOT, "protean-ingredient.json"), encoding="utf-8") as fh:
            descriptor = json.load(fh)
        names = [g["name"] for g in descriptor["gates"]]
        self.assertIn("op-contrib-state", names)
        cmds = [g["cmd"] for g in descriptor["gates"]]
        self.assertTrue(any("check-contrib-state.py" in c for c in cmds), cmds)
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as fh:
            installer = fh.read()
        self.assertIn('VERSION="%s"' % descriptor["version"], installer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
