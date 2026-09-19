#!/usr/bin/env python3
"""Focused fixtures for the five control-plane gates (H2). Not a framework.

Each gate gets at least one GREEN fixture and one RED fixture; RED cases are
single-cause so a failure names exactly the rule under test. Run:

  python3 -m unittest discover -s tests
"""
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "protean-ops"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GATES = ["check-rotation.py", "check-inflight.py", "check-learnings.py",
         "check-decision-report.py", "check-hotpath-freeze.py"]

ROT_HDR = ("| handoff id | boundary time | previous worker | next worker | stage / unit "
           "| capability justification | verified provider/model | exception reason | release state |\n"
           "|---|---|---|---|---|---|---|---|---|\n")


def rot_row(hid, prev, nxt, model, exc="none"):
    return ("| %s | 2026-09-17T00:00:00Z | %s | %s | s4 build | cap-just | %s | %s | released |\n"
            % (hid, prev, nxt, model, exc))


INF_HDR = ("| claim id | session id | profile | provider/model | tree / worktree / branch "
           "| owned files | claim time | lease expiry | release time | conflict result | status |\n"
           "|---|---|---|---|---|---|---|---|---|---|---|\n")


def inf_row(cid, files, expiry="2099-01-01T00:00:00Z", rel="—", status="active",
            tree="`workspace` main"):
    return ("| %s | s-%s | build | example-provider / model-x | %s | %s "
            "| 2026-09-17T00:00:00Z | %s | %s | none | %s |\n"
            % (cid, cid, tree, files, expiry, rel, status))


LRN_HDR = ("| learning id | incident | root cause | one bounded rule | canonical home "
           "| enforcement surface | independent verifier | status | evidence path |\n"
           "|---|---|---|---|---|---|---|---|---|\n")


def lrn_row(lid, verifier="qa (auditor never author)"):
    return ("| %s | incident text | root cause text | one rule text | SKILL.md §x "
            "| gate G-x: check-something.py | %s | open | cache/x-receipt.md |\n" % (lid, verifier))


class GateRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.tmp / "ROSTER.txt").write_text(
            "roles: qa, build, research, architecture, design\n", encoding="utf-8")

    def write(self, name, text):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run_gate(self, gate, *argv):
        r = subprocess.run([sys.executable, str(SCRIPTS / gate)] + [str(a) for a in argv],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


class TestHelp(GateRun):
    def test_help_exit0(self):
        for g in GATES:
            rc, out = self.run_gate(g, "--help")
            self.assertEqual(rc, 0, "%s --help rc=%d" % (g, rc))
            self.assertIn("Usage", out, g)


class TestRotation(GateRun):
    def fixture(self, rows):
        return self.write("ROT.md", "# T\n\n## Rows\n\n" + ROT_HDR + rows)

    def test_green(self):
        p = self.fixture(rot_row("a-h1", "qa", "build", "example-provider / model-x")
                         + rot_row("a-h2", "build", "qa", "example-provider / model-y"))
        rc, out = self.run_gate("check-rotation.py", p)
        self.assertEqual(rc, 0, out)
        self.assertIn("check-rotation: PASS", out)

    def test_red_consecutive_without_exception(self):
        p = self.fixture(rot_row("a-h1", "qa", "build", "example-provider / model-x")
                         + rot_row("a-h2", "build", "build", "example-provider / model-x"))
        rc, out = self.run_gate("check-rotation.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("consecutive assignment to 'build'", out)

    def test_green_consecutive_with_exception(self):
        p = self.fixture(rot_row("a-h1", "qa", "build", "example-provider / model-x")
                         + rot_row("a-h2", "build", "build", "example-provider / model-x",
                                   exc="capability: only builder; no handoff boundary"))
        rc, out = self.run_gate("check-rotation.py", p)
        self.assertEqual(rc, 0, out)

    def test_red_off_roster_name(self):
        p = self.fixture(rot_row("a-h1", "outsider", "build", "example-provider / model-x"))
        rc, out = self.run_gate("check-rotation.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("not on the roster", out)

    def test_red_empty_model(self):
        p = self.fixture(rot_row("a-h1", "qa", "build", "—"))
        rc, out = self.run_gate("check-rotation.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("provider/model receipt", out)

    def test_red_missing_file(self):
        rc, out = self.run_gate("check-rotation.py", self.tmp / "absent.md")
        self.assertEqual(rc, 2)


class TestInflight(GateRun):
    def fixture(self, rows):
        return self.write("INF.md", "# T\n\n## Lease rows\n\n" + INF_HDR
                          + "| (empty state) | — | — | — | — | — | — | — | — | — | empty |\n"
                          + rows
                          + "<!-- Example, ILLUSTRATIVE ONLY:\n"
                          + inf_row("inf-X", "`scripts/x.py`")
                          + "-->\n")

    def test_green_disjoint_and_ignored_rows(self):
        p = self.fixture(inf_row("inf-A", "`scripts/a.py`")
                         + inf_row("inf-B", "`scripts/b.py`")
                         + inf_row("inf-R", "`scripts/c.py`", rel="2026-09-17T02:00:00Z",
                                   status="released"))
        rc, out = self.run_gate("check-inflight.py", p)
        self.assertEqual(rc, 0, out)
        self.assertIn("check-inflight: PASS", out)
        self.assertNotIn("inf-X", out)  # illustrative comment ignored

    def test_red_overlap_same_tree(self):
        p = self.fixture(inf_row("inf-A", "`scripts/a.py`")
                         + inf_row("inf-B", "`scripts/a.py`, `scripts/b.py`"))
        rc, out = self.run_gate("check-inflight.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("overlapping active writers", out)
        self.assertIn("scripts/a.py", out)

    def test_green_same_file_different_tree(self):
        p = self.fixture(inf_row("inf-A", "`scripts/a.py`")
                         + inf_row("inf-B", "`scripts/a.py`", tree="`~/other-repo` main"))
        rc, out = self.run_gate("check-inflight.py", p)
        self.assertEqual(rc, 0, out)

    def test_red_expired_active_claim(self):
        p = self.fixture(inf_row("inf-A", "`scripts/a.py`", expiry="2020-01-01T00:00:00Z"))
        rc, out = self.run_gate("check-inflight.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("past lease expiry", out)


class TestLearnings(GateRun):
    def fixture(self, rows):
        return self.write("LRN.md", "# T\n\n## Rows\n\n" + LRN_HDR + rows)

    def test_green(self):
        p = self.fixture(lrn_row("lrn-001"))
        rc, out = self.run_gate("check-learnings.py", p)
        self.assertEqual(rc, 0, out)
        self.assertIn("check-learnings: PASS", out)

    def test_red_placeholder_field(self):
        p = self.fixture(lrn_row("lrn-002", verifier="—"))
        rc, out = self.run_gate("check-learnings.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("independent verifier", out)

    def test_red_duplicate_id(self):
        p = self.fixture(lrn_row("lrn-001") + lrn_row("lrn-001"))
        rc, out = self.run_gate("check-learnings.py", p)
        self.assertEqual(rc, 1)
        self.assertIn("duplicate learning id", out)


class TestDecisionReport(GateRun):
    HTML = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>body{color:#000}"
            "</style></head><body><h1>Decision</h1></body></html>\n")

    def test_green(self):
        rpt = self.write("reports/decision.html", self.HTML)
        art = self.write("changes/x.md", "artifact\n")
        man = self.write("manifest.txt", "# decision artifacts\n%s\n%s\n" % (art, rpt))
        ev = self.write("evidence.md", "see decision.html for the report\n")
        rc, out = self.run_gate("check-decision-report.py", rpt,
                                "--manifest", man, "--evidence", ev)
        self.assertEqual(rc, 0, out)
        self.assertIn("check-decision-report: PASS", out)

    def test_green_manifest_supplied_reports(self):
        rpt = self.write("reports/decision.html", self.HTML)
        self.write("changes/x.md", "ok\n")
        man = self.write("manifest.txt", "changes/x.md\nreports/decision.html\n")
        rc, out = self.run_gate("check-decision-report.py", "--manifest", man)
        self.assertEqual(rc, 0, out)

    def test_red_external_asset(self):
        bad = self.write("reports/bad.html", self.HTML.replace(
            "<style>", "<link rel=\"stylesheet\" href=\"https://cdn.example/x.css\"><style>"))
        ev = self.write("evidence.md", "bad.html referenced\n")
        rc, out = self.run_gate("check-decision-report.py", bad, "--evidence", ev)
        self.assertEqual(rc, 1)
        self.assertIn("external asset", out)

    def test_red_unlinked_report(self):
        rpt = self.write("reports/decision.html", self.HTML)
        rc, out = self.run_gate("check-decision-report.py", rpt)
        self.assertEqual(rc, 1)
        self.assertIn("not referenced", out)

    def test_red_not_html(self):
        md = self.write("notes.md", "# plain markdown, not a report\n")
        rc, out = self.run_gate("check-decision-report.py", md,
                                "--evidence", self.write("e.md", "notes.md"))
        self.assertEqual(rc, 1)
        self.assertIn("not an HTML file", out)

    def test_red_missing_report(self):
        rc, out = self.run_gate("check-decision-report.py", self.tmp / "nope.html",
                                "--evidence", self.write("e.md", "nope.html"))
        self.assertEqual(rc, 1)
        self.assertIn("missing", out)

    def test_red_no_inputs(self):
        rc, out = self.run_gate("check-decision-report.py")
        self.assertEqual(rc, 2)


class TestHotpathFreeze(GateRun):
    def manifest(self, pairs):
        lines = ["# hot-path manifest\n"] + ["%s  %s" % (h, p) for h, p in pairs]
        return self.write("hot.md5", "\n".join(lines) + "\n")

    def md5(self, p):
        return hashlib.md5(Path(p).read_bytes()).hexdigest()

    def test_green(self):
        a = self.write("home/p/SOUL.md", "identity router\n")
        b = self.write("home/p/memories/MEMORY.md", "breadcrumb\n")
        m = self.manifest([(self.md5(a), a), (self.md5(b), b)])
        rc, out = self.run_gate("check-hotpath-freeze.py", m)
        self.assertEqual(rc, 0, out)
        self.assertIn("intact=2", out)

    def test_red_changed_without_receipt(self):
        a = self.write("home/p/SOUL.md", "identity router\n")
        m = self.manifest([(self.md5(a), a)])
        a.write_text("bloating happened\n", encoding="utf-8")
        rc, out = self.run_gate("check-hotpath-freeze.py", m)
        self.assertEqual(rc, 1)
        self.assertIn("changed without receipt", out)

    def test_green_changed_with_receipt(self):
        a = self.write("home/p/SOUL.md", "identity router\n")
        m = self.manifest([(self.md5(a), a)])
        a.write_text("gate-passed update\n", encoding="utf-8")
        rec = self.write("change-receipt.md", "accepted: G-1 rerun, byte cap ok\n")
        rc, out = self.run_gate("check-hotpath-freeze.py", m, "--allow-change", rec)
        self.assertEqual(rc, 0, out)
        self.assertIn("allowed by receipt", out)
        self.assertIn("check-hotpath-freeze: PASS", out)

    def test_red_missing_file_and_manifest(self):
        a = self.write("home/p/SOUL.md", "x\n")
        gone = self.tmp / "profiles" / "p" / "USER.md"
        m = self.manifest([(self.md5(a), a), ("0" * 32, gone)])
        rc, out = self.run_gate("check-hotpath-freeze.py", m)
        self.assertEqual(rc, 1)
        self.assertIn("file missing", out)
        rc, out = self.run_gate("check-hotpath-freeze.py", self.tmp / "absent.md5")
        self.assertEqual(rc, 2)


class TestShippedTemplates(unittest.TestCase):
    """The shipped schema templates must be the records the gates parse.

    The documented first step for an operator is to copy a template to the record
    name its gate expects and then append rows. A template that does not carry the
    record's table header cannot become a checkable record that way, and the
    header is the one part of the record no role should have to retype.
    """

    ROOT = Path(__file__).resolve().parent.parent
    TEMPLATES = ROOT / "records"

    def gate_module(self, gate):
        spec = importlib.util.spec_from_file_location(
            "gate_" + gate.replace("-", "_")[:-3], SCRIPTS / gate)
        assert spec is not None and spec.loader is not None, gate
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def table_header(self, text, heading):
        """First non-separator table row under `heading`, or None."""
        started = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                started = s.startswith(heading)
                continue
            if not started or not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            if all(c and set(c) <= set("-: ") for c in cells):
                continue
            return cells
        return None

    def copied_record(self, template, record, extra=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / record).write_text(
            (self.TEMPLATES / template).read_text(encoding="utf-8"), encoding="utf-8")
        for name, text in extra:
            (root / name).write_text(text, encoding="utf-8")
        return root

    def run_gate(self, gate, *argv):
        r = subprocess.run([sys.executable, str(SCRIPTS / gate)] + [str(a) for a in argv],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def assert_template_table(self, template, heading, names=None, count=None):
        header = self.table_header(
            (self.TEMPLATES / template).read_text(encoding="utf-8"), heading)
        self.assertIsNotNone(
            header, "%s: no table under '%s'" % (template, heading))
        assert header is not None
        if count is not None:
            self.assertEqual(len(header), count,
                             "%s: %d columns, expected %d" % (template, len(header), count))
        if names is not None:
            self.assertEqual([c.lower() for c in header], [c.lower() for c in names],
                             "%s: column names differ from the gate's own column set" % template)

    def test_rotation_template_carries_the_gate_columns(self):
        self.assert_template_table(
            "ROTATION-STATE.md.tmpl", "## Rows",
            names=self.gate_module("check-rotation.py").COLS)

    def test_learnings_template_carries_the_gate_columns(self):
        self.assert_template_table(
            "LEARNINGS.md.tmpl", "## Rows",
            names=self.gate_module("check-learnings.py").FIELDS)

    def test_inflight_template_carries_the_gate_columns(self):
        self.assert_template_table(
            "INFLIGHT.md.tmpl", "## Lease rows",
            count=self.gate_module("check-inflight.py").NCOL)

    def test_contrib_state_template_carries_the_gate_columns(self):
        self.assert_template_table(
            "CONTRIB-STATE.md.tmpl", "## Rows",
            count=self.gate_module("check-contrib-state.py").NCOL)

    def test_copied_inflight_template_is_checkable(self):
        root = self.copied_record("INFLIGHT.md.tmpl", "INFLIGHT.md")
        rc, out = self.run_gate("check-inflight.py", root / "INFLIGHT.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("check-inflight: PASS", out)

    def test_copied_contrib_state_template_is_checkable(self):
        root = self.copied_record("CONTRIB-STATE.md.tmpl", "CONTRIB-STATE.md")
        rc, out = self.run_gate("check-contrib-state.py", root / "CONTRIB-STATE.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("check-contrib-state: PASS", out)

    def test_copied_rotation_template_reports_missing_input_not_a_violation(self):
        root = self.copied_record(
            "ROTATION-STATE.md.tmpl", "ROTATION-STATE.md",
            [("ROSTER.txt", "roles: qa, build\n")])
        rc, out = self.run_gate("check-rotation.py", root / "ROTATION-STATE.md")
        # A rotation record with no handoff row yet is unverifiable, not invalid:
        # the gate reports missing input (2), never a violation (1).
        self.assertEqual(rc, 2, out)
        self.assertIn(str(root / "ROTATION-STATE.md"), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
