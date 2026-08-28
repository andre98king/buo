#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test G7: persistenza fan al boot (modules-load.d + modprobe.d)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.fix import fan as fan_mod
from buo.fix.fan import FanControl


class TestFanPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mload = Path(self._tmp.name) / "modules-load.d"
        self.mopt = Path(self._tmp.name) / "modprobe.d"
        self.mload.mkdir()
        self.mopt.mkdir()
        self.mload_c = self.mload / "nct6683.conf"
        self.mopt_c = self.mopt / "nct6683.conf"

    def tearDown(self):
        self._tmp.cleanup()

    def test_persist_writes_both_files(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c):
            fan = FanControl()
            ok = fan._persist()
        self.assertTrue(ok)
        self.assertEqual(self.mload_c.read_text(), "nct6683\n")
        self.assertEqual(self.mopt_c.read_text(),
                         "options nct6683 force=true\n")

    def test_apply_persists_when_modprobe_ok(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command",
                        return_value=(0, "", "")):
            fan = FanControl()
            out = fan.apply()
        self.assertTrue(out["applied"])
        self.assertTrue(out["persisted"])
        self.assertTrue(self.mload_c.exists())

    def test_apply_not_persist_when_modprobe_fails(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command",
                        return_value=(1, "", "boom")):
            fan = FanControl()
            out = fan.apply()
        self.assertFalse(out["applied"])
        self.assertFalse(out["persisted"])

    def test_rollback_removes_persistence_files(self):
        self.mload_c.write_text("nct6683\n")
        self.mopt_c.write_text("options nct6683 force=true\n")
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command", return_value=(0, "", "")):
            fan = FanControl()
            ok = fan.rollback()
        self.assertTrue(ok)
        self.assertFalse(self.mload_c.exists())
        self.assertFalse(self.mopt_c.exists())


if __name__ == "__main__":
    unittest.main()
