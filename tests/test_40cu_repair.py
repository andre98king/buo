#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test G4: auto-riparazione unità 40-CU (BUGS #24)."""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator

UNIT = "bc250-cu-live-manager"


class Test40CuAutoRepair(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        return Orchestrator(config=cfg, mock=False, dry_run=False)

    def test_repair_enables_existing_disabled_unit(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "cat"]:
                return 0, "", ""
            if cmd[:2] == ["systemctl", "enable"]:
                return 0, "", ""
            return 1, "", ""

        orch = self._make()
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            ok = orch._repair_40cu_service(UNIT)
        self.assertTrue(ok)
        self.assertTrue(any(c[:2] == ["systemctl", "enable"] for c in calls))
        # nessuna reinstall (unità già presente)
        self.assertFalse(any("install-service" in c for c in calls))

    def test_repair_reinstalls_missing_unit(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "cat"]:
                return 1, "", "unit not found"
            return 0, "", ""

        orch = self._make()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("shutil.copy2"), \
             mock.patch("os.remove"), \
             mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            ok = orch._repair_40cu_service(UNIT)
        self.assertTrue(ok)
        installs = [c for c in calls if "install-service" in c]
        self.assertEqual(len(installs), 1)
        # quirk: si esegue dalla copia in /tmp, non da /usr/local/bin
        self.assertEqual(installs[0][0], "/tmp/bc250-cu-live-manager")
        self.assertTrue(any("apply-service" in c for c in calls))

    def test_repair_fails_without_binary(self):
        def fake_run(cmd, **kw):
            if cmd[:2] == ["systemctl", "cat"]:
                return 1, "", "unit not found"
            return 1, "", ""

        orch = self._make()
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            ok = orch._repair_40cu_service(UNIT)
        self.assertFalse(ok)

    def test_check_no_warning_when_enabled(self):
        """Unità abilitata → check pulito, nessuna chiamata di riparazione."""
        orch = self._make()
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(0, "enabled", "")), \
             mock.patch.object(orch, "_repair_40cu_service",
                               side_effect=AssertionError(
                                   "nessuna riparazione se già abilitato")):
            orch._check_40cu_service_enabled()  # non deve sollevare


if __name__ == "__main__":
    unittest.main()
