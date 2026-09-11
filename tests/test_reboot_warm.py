#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Test del reset WARM forzato prima dei reboot di BUO.

Scoperta di campo 11/09/2026: `/sys/kernel/reboot/mode` è `cold` e la maschera
core (registro volatile) sopravvive SOLO al warm reset → senza forzarlo,
l'unlock 8-core andrebbe perso a ogni reboot (boot-loop reale nel port
community con auto-reboot).
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from buo.state.reboot import RebootManager, force_warm_reset


class ForceWarmResetTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.mode = Path(self.tmp.name) / "reboot-mode"
        self.mode.write_text("cold\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_warm(self):
        self.assertTrue(force_warm_reset(self.mode))
        self.assertEqual(self.mode.read_text().strip(), "warm")

    def test_fail_soft_if_not_writable(self):
        """Path non scrivibile → False, mai eccezioni (fail-soft)."""
        bad = Path(self.tmp.name) / "nope" / "reboot-mode"
        self.assertFalse(force_warm_reset(bad))


class ScheduleForcesWarmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.mode = Path(self.tmp.name) / "reboot-mode"
        self.mode.write_text("cold\n")
        self.manager = RebootManager(reboot_mode_path=self.mode)

    def tearDown(self):
        self.tmp.cleanup()

    def _schedule(self):
        with patch.object(RebootManager, "_create_resume_service",
                          return_value=True), \
                patch.object(self.manager, "_run", return_value=(0, "", "")), \
                patch("buo.state.reboot.time.sleep"):
            with self.assertRaises(SystemExit):
                self.manager.schedule("test", delay=0)

    def test_reboot_forces_warm_first(self):
        self._schedule()
        self.assertEqual(self.mode.read_text().strip(), "warm")

    def test_not_writable_logs_warning_and_proceeds(self):
        bad = Path(self.tmp.name) / "nope" / "mode"
        self.manager = RebootManager(reboot_mode_path=bad)
        # LoggerMixin usa "buo.<Classe>": cattura tutto il prefisso "buo"
        with self.assertLogs("buo", level="WARNING") as log:
            self._schedule()
        self.assertTrue(any("NON forzato a warm" in line
                            for line in log.output))


if __name__ == "__main__":
    unittest.main()
