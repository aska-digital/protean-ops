#!/usr/bin/env python3
"""Tests for install.sh: standalone install, dry-run writes nothing."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(args, cwd=None):
    return subprocess.run(["bash", os.path.join(ROOT, "install.sh")] + args,
                          capture_output=True, text=True, cwd=cwd or ROOT,
                          timeout=120)


class TestInstall(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "installed")
            result = run(["--target", target, "--dry-run"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no files written", result.stdout)
            self.assertFalse(os.path.exists(target),
                             "dry-run created the target directory")

    def test_install_copies_declared_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "installed")
            result = run(["--target", target])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("wrote: ", result.stdout)
            for rel in ['gates/protean-ops', 'records', 'scripts/protean-ops']:
                self.assertTrue(os.path.exists(os.path.join(target, rel)),
                                "missing installed path: " + rel)

    def test_missing_target_is_usage_error(self):
        result = run([])
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage", result.stderr)

    def test_unknown_flag_is_usage_error(self):
        result = run(["--target", "/tmp/never-used", "--bogus"])
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
